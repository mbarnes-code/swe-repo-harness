"""Phase 1 step 5b: contract discovery, ownership and hoist candidacy (§3.1 5b, ADR-0019).

This is the step that turns "twelve repos share one proto package" from a fact nobody recorded
into a **node**, and it exists for exactly one downstream reason: `graph/cycles.py` 6c-H can only
dissolve a cycle by hoisting a contract that has already been discovered, ranked and marked
`EXTRACTABLE`. `graph/infer.py::infer_contract_edges` could always turn a `ContractNode` into its
two edge kinds; until this module there was nothing to hand it.

**No model participates.** §3.1 is explicit — "contract extraction, ownership, and hoist ranking
are **forbidden** to the model" — so `ctx.llm` is never touched here, and every judgement is a
predicate over persisted rows and declarative config. That is a stricter rule than CLAUDE.md's
Rule 5 and it wins: where the two disagree, the primary reference document is the authority.

**It opens no file.** §3.1 5b: 5b is "a pure function of rows already persisted … plus two bounded
facts recorded per file during step 4's existing single walk", namely a YAML/JSON document's root
keys and whether a file carries a generated-code marker — both already emitted by
`workers/symbolindex.py` as `SymbolKind.MODULE` rows (`<path>#<key>` and `<path>#generated`). The
only filesystem contact is a **directory listing** through `interrogate.walk_files`, the same walk
every other scan step uses, because §6 persists no path inventory (see PATH UNIVERSE below).

**Per-kind knowledge is a table, not a branch** (§1). Every `ContractKind`-shaped fact lives in one
of the three `Mapping[ContractKind, …]` tables below; there is no `if kind is …` and no `match`.
The `ecosystems/contracts/` `ContractAdapter` registry §1 names does not exist in this tree yet, so
these tables are the registry in miniature and are what should move into it.

PATH UNIVERSE — the one deviation from the letter of §3.1 5b, stated loudly:
§3.1 5b reads "the `ls-tree` path/blob-SHA listing captured at preflight". `workers/clone.py`
captures no such listing and §6 has no table for one, so two things follow, both visible in the
output rather than papered over:

* the path universe is `walk_files(worktree)` UNION the paths this run's `symbols` rows name — a
  union, so a repo whose worktree has been reaped still contributes every file that produced a
  symbol;
* **no blob SHA is available**, therefore `content_sha256` is empty, the `divergent` modifier can
  never fire from real scan data, and a `CONTRACT` collision between byte-identical and divergent
  copies cannot be told apart (it is reported as `warn`). `ContractsInput.blob_shas` exists to be
  fed the listing the moment preflight captures one; the discovery code below already honours it.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Final

from pydantic import Field

from fleet.bazel.layout import is_reserved_dest, normalize_dest
from fleet.graph.collisions import CollisionInput, ContractClaim, audit_collisions
from fleet.models.base import FleetModel
from fleet.models.enums import ContractKind, ContractStatus, Phase, SymbolKind
from fleet.models.graph import CollisionFinding, ContractId, ContractNode, SymbolRef
from fleet.models.repo import RepoId
from fleet.orchestrator.registry import register_worker
from fleet.settings import ContractsSection
from fleet.util.hashing import sha256_text
from fleet.workers.base import (
    BaseWorker,
    WorkerContext,
    WorkerInput,
    WorkerOutput,
    WorkerResult,
)
from fleet.workers.interrogate import is_ignored, walk_files

__all__ = [
    "BASE_EXTRACTION_CONFIDENCE",
    "CONTRACT_SYMBOL_KINDS",
    "IDENTIFIER_SOURCES",
    "MODIFIERS",
    "PREDICATES",
    "ContractExtractionWorker",
    "ContractsInput",
    "ContractsOutput",
    "RepoFacts",
    "carry_over_committed",
    "discover_contracts",
    "hoist_target_path",
]

# =======================================================================================
# the per-kind tables — §1's "a total registry, never a branch", in miniature
# =======================================================================================

IDENTIFIER_SOURCES: Final[Mapping[str, ContractKind]] = {
    ".proto": ContractKind.PROTO,
    ".avsc": ContractKind.AVRO,
    ".avdl": ContractKind.AVRO,
    ".thrift": ContractKind.THRIFT,
}
"""IDL suffix → `ContractKind`, for a path that also matches `scan.contracts.idl_globs`."""

SYMBOL_IDENTIFIED: Final[Mapping[ContractKind, str]] = {ContractKind.PROTO: "proto"}
"""Kind → the `symbols.language` whose `SymbolKind.MODULE` definition row IS the identifier.

Only `PROTO` is here, and that is a **spec gap, not an omission**: §3.1 5b keys `AVRO` on the
`namespace` field and `THRIFT` on the `namespace` directive, and `workers/symbolindex.py` parses
neither (`LANGUAGES` has no `.avsc`/`.avdl`/`.thrift` entry), so no persisted row carries either
value. Reading them would mean opening the file, which 5b may not do. An `.avsc`/`.thrift` under
`idl_globs` is therefore reported in `ContractsOutput.unidentifiable_idl_paths` instead of being
guessed at — a contract whose identity was invented would collapse two unrelated schemas into one
node."""

TARGET_PATH: Final[Mapping[ContractKind, str]] = {
    ContractKind.PROTO: "proto/{dotted}",
    ContractKind.OPENAPI: "contracts/openapi/{slug}",
    ContractKind.AVRO: "contracts/avro/{dotted}",
    ContractKind.THRIFT: "contracts/thrift/{dotted}",
    ContractKind.SHARED_LIB: "{declared}",
}
"""Kind → `hoist_target_path` template (§3.3's table). `{dotted}` is the identifier with `.` → `/`;
`{slug}` folds every non-alphanumeric run to `-`; `{declared}` is the `dest:` from
`scan.contracts.shared_libs`. §3.3 lets a `SHARED_LIB` with no `dest:` fall back to `layout()` of
its owner's primary published coordinate; that needs the `EcosystemRegistry` this module has no
business holding, so such an entry is `REJECTED:hoist_target` with the reason recorded rather than
sent to a path this module made up."""

CONTRACT_SYMBOL_KINDS: Final[tuple[SymbolKind, ...]] = (
    SymbolKind.MODULE,
    SymbolKind.GRPC_SERVICE,
    SymbolKind.PROTO_MESSAGE,
    SymbolKind.HTTP_OPERATION,
    SymbolKind.IMPORT,
)
"""The only `symbols` kinds 5b reads. Exported so the driver's `SELECT` can narrow to them without
restating which rows this step's rules are made of."""

BASE_EXTRACTION_CONFIDENCE: Final = 0.9
"""§3.1 5b (vi): `extraction_confidence = clamp(0.9 × Π modifiers, 0, 1)`."""

MODIFIERS: Final[Mapping[str, float]] = {
    "divergent": 0.5,
    "owner_fallback": 0.9,
    "openapi_path_identified": 0.8,
    "min_consumers": 0.95,
    "spread_sources": 0.8,
}
"""Every §3.1 5b (vi) modifier, by the name it is stored under in `confidence_factors` — the score
must be reconstructible from that dict alone, exactly as `edges.confidence_factors` is."""

PREDICATES: Final[tuple[str, ...]] = (
    "min_consumers",
    "identifier",
    "clean_subtree",
    "imports",
    "hoist_target",
    "extraction_confidence",
)
"""§3.1 5b (vi)'s predicates in the order they are evaluated; the FIRST failure becomes
`status_detail` beside `status = REJECTED`. `hoist_target` is not in §3.1's list because §3.1
assumes `layout()` is total over contract nodes; it is the `SHARED_LIB`-without-`dest:` case above,
and it is a rejection rather than an invented path."""

_UNPACKAGED: Final = "_unpackaged"
_SLUG_ILLEGAL: Final = re.compile(r"[^a-z0-9._/:-]+")
_DEST_ILLEGAL: Final = re.compile(r"[^a-z0-9]+")


# =======================================================================================
# payloads
# =======================================================================================


class RepoFacts(FleetModel):
    """One repo as §3.1 5b (iv)'s ownership ladder sees it. Rows, not a worktree."""

    repo_id: RepoId
    worktree_path: str = Field(
        default="",
        description="Where this repo's tree is on disk, for the path listing. Empty (or reaped) "
        "is not an error: the repo still contributes every path its `symbols` rows name.",
    )
    commit_count: int = Field(default=0, ge=0, description="Ladder rung (iv)")
    owns: tuple[str, ...] = Field(
        default=(),
        description="`config/repos.yaml` `owns:` — ladder rung (i). The SAME list that overrides "
        "coordinate ownership; a `contract_id` here wins outright.",
    )
    publishes_coordinate: bool = Field(
        default=False, description="Ladder rung (ii)'s tie-break toward a publishing repo"
    )


class ContractsInput(WorkerInput):
    """Everything 5b reads. Persisted rows and declarative config — no model, no network."""

    repos: tuple[RepoFacts, ...] = ()
    symbols: tuple[SymbolRef, ...] = ()
    blob_shas: Mapping[str, str] = Field(
        default_factory=dict,
        description="'{repo_id}\\x00{path}' → git blob SHA, from the preflight `ls-tree` listing "
        "§3.1 5b assumes. Empty under today's preflight (see the module docstring); supplying it "
        "is the whole fix for `content_sha256` and the `divergent` modifier.",
    )
    committed: tuple[ContractNode, ...] = Field(
        default=(),
        description="`HOISTED`/`MIGRATED` rows that must survive the whole-run rebuild (§3.1 "
        "'Contract extraction is re-runnable by construction'), re-applied by "
        "`carry_over_committed`",
    )
    config: ContractsSection = ContractsSection()
    ignore_globs: tuple[str, ...] = ()
    vendor_globs: tuple[str, ...] = ()
    generated_globs: tuple[str, ...] = ()
    min_extraction_confidence: float = Field(default=0.6, ge=0.0, le=1.0)


class ContractsOutput(WorkerOutput):
    """The `contracts` rows and the step-8 `CONTRACT` collision rows, for the driver to persist."""

    contracts: tuple[ContractNode, ...] = ()
    collisions: tuple[CollisionFinding, ...] = ()
    unidentifiable_idl_paths: tuple[str, ...] = Field(
        default=(),
        description="'{repo_id}:{path}' for every IDL file whose identity no persisted row "
        "carries (see `SYMBOL_IDENTIFIED`). Data, not silence: Rule 11.",
    )


# =======================================================================================
# the worker
# =======================================================================================


@register_worker
class ContractExtractionWorker(BaseWorker[ContractsInput, ContractsOutput]):
    """§3.1 step 5b, as one fleet-wide dispatch.

    Fleet-wide rather than per-repo on purpose: a contract spans repos, so per-repo scoping would
    make `proto:acme.identity.v1` nine rows in nine transactions instead of one row keyed
    `(run_id, kind, identifier)`. §3.1 says so directly — 5b "is instead re-derived for the whole
    run in one transaction".
    """

    __slots__ = ()

    name: ClassVar[str] = "contracts"
    phase: ClassVar[Phase] = Phase.SCAN
    input_model: ClassVar[type[WorkerInput]] = ContractsInput
    output_model: ClassVar[type[WorkerOutput]] = ContractsOutput

    async def preconditions_hold(self, ctx: WorkerContext, payload: ContractsInput) -> bool:
        """Always True, and this is a claim rather than a default (see `BaseWorker`).

        Re-entry is safe here because 5b is a **rebuild**, not an accumulation: it re-derives every
        row from `symbols` plus config, keyed `(run_id, contract_id)`, so running it twice writes
        the same rows twice rather than nine copies of a vendored proto. The one thing a rebuild
        must not lose — a contract 6c-H already hoisted — is not recomputed at all: it arrives on
        `payload.committed` and is re-applied by `carry_over_committed`.
        """
        _ = (ctx, payload)
        return True

    async def run(
        self, ctx: WorkerContext, payload: ContractsInput
    ) -> WorkerResult[ContractsOutput]:
        listings = await asyncio.to_thread(_path_universe, payload)
        output = discover_contracts(payload, listings)
        ctx.log.info(
            "contracts.extracted",
            contracts=len(output.contracts),
            extractable=sum(1 for c in output.contracts if c.extractable),
            collisions=len(output.collisions),
            unidentifiable=len(output.unidentifiable_idl_paths),
        )
        return WorkerResult[ContractsOutput](
            status="ok", output=output, completed_units=[self.name]
        )


# =======================================================================================
# the path universe
# =======================================================================================


def _path_universe(payload: ContractsInput) -> Mapping[str, tuple[str, ...]]:
    """repo_id → every path 5b can see: the worktree walk plus the paths `symbols` names.

    A union rather than either half alone. The walk is the complete picture but only while the
    worktree survives; the symbol paths outlive it and are the only evidence for a repo whose scan
    landed in an earlier invocation. Both halves are sorted, so the union is reproducible.
    """
    by_repo: dict[str, set[str]] = {facts.repo_id: set() for facts in payload.repos}
    for symbol in payload.symbols:
        by_repo.setdefault(symbol.repo_id, set()).add(symbol.path)
    for facts in payload.repos:
        root = Path(facts.worktree_path) if facts.worktree_path else None
        if root is not None and root.is_dir():
            by_repo[facts.repo_id].update(walk_files(root, payload.ignore_globs))
    return {repo_id: tuple(sorted(paths)) for repo_id, paths in sorted(by_repo.items())}


# =======================================================================================
# discovery — a pure function, so two processes agree byte for byte (§11.6)
# =======================================================================================


@dataclass(frozen=True, slots=True)
class _Carrier:
    """One repo's copy of one contract's source file."""

    repo_id: str
    path: str
    generated: bool
    vendored: bool

    @property
    def depth(self) -> int:
        return self.path.count("/")

    @property
    def directory(self) -> str:
        head, sep, _ = self.path.rpartition("/")
        return head if sep else ""


@dataclass(frozen=True, slots=True)
class _Index:
    """The `symbols` rows 5b joins on, indexed once."""

    generated: frozenset[tuple[str, str]]
    root_keys: Mapping[tuple[str, str], frozenset[str]]
    packages: Mapping[tuple[str, str], str]
    imports: Mapping[tuple[str, str], tuple[str, ...]]
    references: tuple[SymbolRef, ...]


def discover_contracts(
    payload: ContractsInput, listings: Mapping[str, Sequence[str]]
) -> ContractsOutput:
    """§3.1 5b (i)–(vi) over one fleet. Pure: same inputs ⇒ byte-identical output, any process."""
    index = _index_symbols(payload.symbols)
    facts = {repo.repo_id: repo for repo in payload.repos}
    carriers, unidentifiable = _discover_carriers(payload, listings, index)
    contract_kinds = _kinds_by_id(payload, listings, index)
    source_dirs = _all_source_directories(carriers)

    nodes: list[ContractNode] = []
    claims: list[ContractClaim] = []
    for contract_id in sorted(carriers):
        rows = carriers[contract_id]
        sources = tuple(c for c in rows if not c.generated)
        if not sources:
            # §3.1 5b (ii): generated code is evidence of consumption, NEVER of ownership. A
            # package that only ever appears as somebody's `*_pb2.py` is published outside this
            # fleet, and inventing a node for it would give the DAG a member no repo can migrate.
            continue
        kind, identifier = contract_kinds[contract_id]
        node = _build_node(
            payload,
            contract_id=contract_id,
            kind=kind,
            identifier=identifier,
            sources=sources,
            generated=_generated_paths(payload, listings, index, identifier, rows),
            index=index,
            facts=facts,
            listings=listings,
            source_dirs=source_dirs,
        )
        nodes.append(node)
        claims.extend(_claims(payload, node, sources))

    report = audit_collisions(
        CollisionInput(
            contracts=tuple(claims),
            owns_hints=_owns_hints(payload, {n.contract_id for n in nodes}),
            vendor_repo_ids=tuple(
                sorted({c.repo_id for rows in carriers.values() for c in rows if c.vendored})
            ),
        )
    )
    return ContractsOutput(
        contracts=carry_over_committed(nodes, payload.committed),
        collisions=report.collisions,
        unidentifiable_idl_paths=unidentifiable,
    )


def carry_over_committed(
    fresh: Iterable[ContractNode], committed: Iterable[ContractNode]
) -> tuple[ContractNode, ...]:
    """Re-apply `HOISTED`/`MIGRATED` (and `hoist_target_path`) onto a rebuilt row set.

    §3.1: a hoist is already committed in git and named by an open PR, so it "MUST survive that
    cycle" — 6c-H treats an already-`HOISTED` contract as pre-committed and never re-ranks it. A
    committed contract that the rebuild no longer discovers is kept as its own row rather than
    dropped: the files are in the monorepo either way, and a vanished row would make
    `COUNT(contracts WHERE status IN ('HOISTED','MIGRATED')) == COUNT(wave_members …)` a lie.
    """
    survivors = {
        node.contract_id: node
        for node in committed
        if node.status in (ContractStatus.HOISTED, ContractStatus.MIGRATED)
    }
    out: dict[str, ContractNode] = {}
    for node in fresh:
        prior = survivors.pop(node.contract_id, None)
        out[node.contract_id] = (
            node
            if prior is None
            else node.model_copy(
                update={
                    "status": prior.status,
                    "hoist_target_path": prior.hoist_target_path,
                    "extractable": True,  # the CHECK a HOISTED row already satisfied
                    "status_detail": prior.status_detail,
                }
            )
        )
    for contract_id, prior in survivors.items():
        out[contract_id] = prior
    return tuple(out[key] for key in sorted(out))


def hoist_target_path(kind: ContractKind, identifier: str, declared: str | None) -> str | None:
    """§3.3's table, applied. None when the kind's template cannot be filled (see `TARGET_PATH`)."""
    template = TARGET_PATH[kind]
    if "{declared}" in template:
        return None if not declared else normalize_dest(declared)
    dest = template.format(
        dotted=identifier.replace(".", "/"),
        slug=_DEST_ILLEGAL.sub("-", identifier.casefold()).strip("-"),
    )
    normalized = normalize_dest(dest)
    return None if is_reserved_dest(normalized) else normalized


# ---------------------------------------------------------------------------------------
# (i) discovery
# ---------------------------------------------------------------------------------------


def _index_symbols(symbols: Sequence[SymbolRef]) -> _Index:
    """The two bounded per-file facts step 4 already recorded, plus the reference rows (v) joins."""
    generated: set[tuple[str, str]] = set()
    root_keys: dict[tuple[str, str], set[str]] = {}
    packages: dict[tuple[str, str], str] = {}
    imports: dict[tuple[str, str], list[str]] = {}
    references: list[SymbolRef] = []
    for symbol in sorted(symbols, key=lambda s: (s.repo_id, s.path, s.line, s.fqn, s.kind.value)):
        key = (symbol.repo_id, symbol.path)
        if symbol.kind is SymbolKind.MODULE and symbol.fqn.startswith(f"{symbol.path}#"):
            marker = symbol.fqn.partition("#")[2]
            if marker == "generated":
                generated.add(key)
            else:
                root_keys.setdefault(key, set()).add(marker.casefold())
        elif (
            symbol.kind is SymbolKind.MODULE
            and symbol.is_definition
            and symbol.language == SYMBOL_IDENTIFIED[ContractKind.PROTO]
        ):
            packages.setdefault(key, symbol.fqn)
        elif symbol.kind is SymbolKind.IMPORT and symbol.language == "proto":
            imports.setdefault(key, []).append(symbol.fqn)
        elif not symbol.is_definition and symbol.kind in (
            SymbolKind.GRPC_SERVICE,
            SymbolKind.PROTO_MESSAGE,
            SymbolKind.HTTP_OPERATION,
        ):
            references.append(symbol)
    return _Index(
        generated=frozenset(generated),
        root_keys={key: frozenset(value) for key, value in sorted(root_keys.items())},
        packages=dict(sorted(packages.items())),
        imports={key: tuple(value) for key, value in sorted(imports.items())},
        references=tuple(references),
    )


def _identify(
    payload: ContractsInput, index: _Index, repo_id: str, path: str
) -> tuple[ContractKind, str] | None:
    """One path → the `(kind, identifier)` it carries, or None. The whole of §3.1 5b (i)'s table."""
    suffix = Path(path).suffix.casefold()
    kind = IDENTIFIER_SOURCES.get(suffix)
    if kind is not None and is_ignored(path, payload.config.idl_globs):
        if kind not in SYMBOL_IDENTIFIED:
            return None  # identity not derivable from any row — reported, never guessed
        package = index.packages.get((repo_id, path))
        if package:
            return kind, package.casefold()
        # §3.1 5b (i): a `.proto` with no `package` gets `_unpackaged.<repo_id>.<dir>` and is
        # forced non-extractable by the `identifier` predicate — never silently merged with the
        # other unpackaged protos in the fleet.
        head, sep, _ = path.rpartition("/")
        return kind, f"{_UNPACKAGED}.{repo_id}.{head if sep else '.'}".casefold()
    roots = index.root_keys.get((repo_id, path), frozenset())
    if roots & {root.casefold() for root in payload.config.openapi_roots}:
        # §3.1 5b (i): `slug(info.title) + ':' + info.version` when both are present. Neither is:
        # step 4 records ROOT keys only, and reaching `info.title` means parsing the document,
        # which 5b may not do. The repo-relative path is §3.1's own stated fallback, and it costs
        # the `openapi_path_identified` modifier every time.
        return ContractKind.OPENAPI, path.casefold()
    return None


def _discover_carriers(
    payload: ContractsInput,
    listings: Mapping[str, Sequence[str]],
    index: _Index,
) -> tuple[Mapping[str, list[_Carrier]], tuple[str, ...]]:
    """Every carrier of every contract, grouped by `contract_id` (§3.1 5b i, iii)."""
    carriers: dict[str, list[_Carrier]] = {}
    unidentifiable: list[str] = []
    for repo_id, paths in sorted(listings.items()):
        for path in paths:
            found = _identify(payload, index, repo_id, path)
            if found is None:
                suffix = Path(path).suffix.casefold()
                if suffix in IDENTIFIER_SOURCES and is_ignored(path, payload.config.idl_globs):
                    unidentifiable.append(f"{repo_id}:{path}")
                continue
            kind, identifier = found
            carriers.setdefault(_contract_id(kind, identifier), []).append(
                _Carrier(
                    repo_id=repo_id,
                    path=path,
                    generated=_is_generated(payload, index, repo_id, path),
                    vendored=is_ignored(path, payload.vendor_globs),
                )
            )
    for entry in sorted(payload.config.shared_libs, key=lambda e: (e.repo, e.identifier)):
        contract_id = _contract_id(ContractKind.SHARED_LIB, entry.identifier.casefold())
        for path in sorted(set(entry.paths)):
            carriers.setdefault(contract_id, []).append(
                _Carrier(repo_id=entry.repo, path=path, generated=False, vendored=False)
            )
    return (
        {key: sorted(rows, key=lambda c: (c.repo_id, c.path)) for key, rows in carriers.items()},
        tuple(sorted(unidentifiable)),
    )


def _kinds_by_id(
    payload: ContractsInput, listings: Mapping[str, Sequence[str]], index: _Index
) -> Mapping[str, tuple[ContractKind, str]]:
    """`contract_id` → `(kind, identifier)`. Re-derived rather than carried on `_Carrier`, so the
    id and the pair it encodes can never drift apart."""
    out: dict[str, tuple[ContractKind, str]] = {}
    for repo_id, paths in sorted(listings.items()):
        for path in paths:
            found = _identify(payload, index, repo_id, path)
            if found is not None:
                out.setdefault(_contract_id(*found), found)
    for entry in payload.config.shared_libs:
        pair = (ContractKind.SHARED_LIB, entry.identifier.casefold())
        out.setdefault(_contract_id(*pair), pair)
    return out


def _contract_id(kind: ContractKind, identifier: str) -> ContractId:
    """`'{kind.lower()}:{identifier}'`, case-folded (§3.1 5b iii), through the `ContractId` charset.

    The slug is not cosmetic: `ContractId` is a validated pattern, so an OpenAPI document at
    `docs/user_api.yaml` (an underscore) would otherwise raise inside the model rather than become
    a node. Folding is total and deterministic, so two runs agree.
    """
    return f"{kind.value.casefold()}:{_slug(identifier)}"


def _slug(text: str) -> str:
    return _SLUG_ILLEGAL.sub("-", text.strip().casefold()).strip("-") or "unnamed"


def _is_generated(payload: ContractsInput, index: _Index, repo_id: str, path: str) -> bool:
    """§3.1 5b (ii). Either half is enough: the glob, or the marker step 4 recorded."""
    return is_ignored(path, payload.generated_globs) or (repo_id, path) in index.generated


# ---------------------------------------------------------------------------------------
# (iii)–(vi) one node
# ---------------------------------------------------------------------------------------


def _build_node(
    payload: ContractsInput,
    *,
    contract_id: str,
    kind: ContractKind,
    identifier: str,
    sources: Sequence[_Carrier],
    generated: Sequence[tuple[str, str]],
    index: _Index,
    facts: Mapping[str, RepoFacts],
    listings: Mapping[str, Sequence[str]],
    source_dirs: Mapping[str, frozenset[str]],
) -> ContractNode:
    owner, by_hint = _owner(contract_id, sources, facts)
    consumers = _consumers(
        payload,
        identifier=identifier,
        owner=owner,
        sources=sources,
        generated=generated,
        index=index,
    )
    shas = sorted({sha for sha in (_blob_sha(payload, c) for c in sources) if sha})
    factors = _factors(
        payload,
        by_hint=by_hint,
        kind=kind,
        consumers=len(consumers),
        directories={c.directory for c in sources},
        distinct_shas=len(shas),
    )
    confidence = min(1.0, max(0.0, BASE_EXTRACTION_CONFIDENCE * _product(factors)))
    declared = next(
        (
            entry.dest
            for entry in payload.config.shared_libs
            if _contract_id(ContractKind.SHARED_LIB, entry.identifier.casefold()) == contract_id
        ),
        None,
    )
    target = hoist_target_path(kind, identifier, declared)
    failed = _first_failing_predicate(
        payload,
        identifier=identifier,
        consumers=consumers,
        sources=sources,
        listings=listings,
        source_dirs=source_dirs,
        index=index,
        target=target,
        confidence=confidence,
    )
    extractable = failed is None
    return ContractNode(
        contract_id=contract_id,
        kind=kind,
        identifier=identifier,
        owning_repo_id=owner,
        source_paths=[
            {"repo_id": c.repo_id, "path": c.path, "blob_sha": _blob_sha(payload, c)}
            for c in sources
        ],
        generated_paths=[{"repo_id": repo_id, "path": path} for repo_id, path in generated],
        consumer_repo_ids=list(consumers),
        extractable=extractable,
        extraction_confidence=confidence,
        confidence_factors=dict(factors),
        content_sha256="" if not shas else sha256_text("\x00".join(shas)),
        hoist_target_path=target if extractable else None,
        status=ContractStatus.EXTRACTABLE if extractable else ContractStatus.REJECTED,
        status_detail="" if failed is None else failed,
    )


def _owner(
    contract_id: str, sources: Sequence[_Carrier], facts: Mapping[str, RepoFacts]
) -> tuple[str, bool]:
    """§3.1 5b (iv), rung by rung. Returns `(owner, chosen_by_owns_hint)`."""
    hinted = sorted(
        {c.repo_id for c in sources if contract_id in facts.get(c.repo_id, _NO_FACTS).owns}
    )
    if hinted:
        return hinted[0], True
    # (ii) prefer a carrier whose copy is neither vendored nor generated; only if EVERY copy is
    # one of those does the ladder fall back to the whole carrier set — an owner must exist.
    eligible = [c for c in sources if not c.vendored and not c.generated] or list(sources)
    ranked = sorted(
        eligible,
        key=lambda c: (
            not facts.get(c.repo_id, _NO_FACTS).publishes_coordinate,
            c.depth,
            -facts.get(c.repo_id, _NO_FACTS).commit_count,
            c.repo_id,
        ),
    )
    return ranked[0].repo_id, False


def _consumers(
    payload: ContractsInput,
    *,
    identifier: str,
    owner: str,
    sources: Sequence[_Carrier],
    generated: Sequence[tuple[str, str]],
    index: _Index,
) -> tuple[str, ...]:
    """§3.1 5b (v). Three joins, all deterministic.

    KNOWN GAP, and it is a model gap rather than a rule gap: a repo detected ONLY by the symbol
    join below holds no path in `source_paths`/`generated_paths`, and `infer_contract_edges` emits
    `CONTRACT_CONSUME` per *evidence path* — so that repo appears in `consumer_repo_ids` and in no
    edge. The fix is a field `ContractNode` does not have (a consumer-evidence path list); the
    alternative — filing the consumer's own hand-written file under `generated_paths` — would mark
    it for DELETION in §3.3, so it is refused. Nothing here fabricates evidence.
    """
    found = {c.repo_id for c in sources if c.repo_id != owner}
    found.update(repo_id for repo_id, _ in generated if repo_id != owner)
    prefix = f"{identifier}."
    found.update(
        symbol.repo_id
        for symbol in index.references
        if symbol.repo_id != owner and symbol.fqn.casefold().startswith(prefix)
    )
    _ = payload
    return tuple(sorted(found))


def _generated_paths(
    payload: ContractsInput,
    listings: Mapping[str, Sequence[str]],
    index: _Index,
    identifier: str,
    rows: Sequence[_Carrier],
) -> tuple[tuple[str, str], ...]:
    """Checked-in generated output belonging to this contract (§3.1 5b ii).

    Two sources: a carrier of the contract's own identity that is itself generated, and a
    generated file whose *emitted package* resolves to the identifier — the dotted identifier as
    directory segments, which is what every protobuf/OpenAPI generator lays down
    (`acme.identity.v1` → `…/acme/identity/v1/…_pb2.py`). Requiring two segments keeps a
    single-token identifier from claiming half the fleet.
    """
    found = {(c.repo_id, c.path) for c in rows if c.generated}
    segments = identifier.split(".")
    if len(segments) >= 2:
        needle = f"/{'/'.join(segments)}/"
        for repo_id, paths in sorted(listings.items()):
            for path in paths:
                if needle in f"/{path}" and _is_generated(payload, index, repo_id, path):
                    found.add((repo_id, path))
    return tuple(sorted(found))


def _factors(
    payload: ContractsInput,
    *,
    by_hint: bool,
    kind: ContractKind,
    consumers: int,
    directories: set[str],
    distinct_shas: int,
) -> Mapping[str, float]:
    """§3.1 5b (vi)'s modifier list, verbatim and in a stable order."""
    factors: dict[str, float] = {}
    if distinct_shas > 1:
        factors["divergent"] = MODIFIERS["divergent"]
    if not by_hint:
        factors["owner_fallback"] = MODIFIERS["owner_fallback"]
    if kind is ContractKind.OPENAPI:
        factors["openapi_path_identified"] = MODIFIERS["openapi_path_identified"]
    if consumers == payload.config.min_consumers:
        factors["min_consumers"] = MODIFIERS["min_consumers"]
    if len(directories) > payload.config.max_source_dirs:
        factors["spread_sources"] = MODIFIERS["spread_sources"]
    return dict(sorted(factors.items()))


def _first_failing_predicate(
    payload: ContractsInput,
    *,
    identifier: str,
    consumers: Sequence[str],
    sources: Sequence[_Carrier],
    listings: Mapping[str, Sequence[str]],
    source_dirs: Mapping[str, frozenset[str]],
    index: _Index,
    target: str | None,
    confidence: float,
) -> str | None:
    """§3.1 5b (vi), in order. The FIRST failure is the recorded reason; None ⇒ extractable."""
    if len(consumers) < payload.config.min_consumers:
        return PREDICATES[0]
    if not identifier or identifier.startswith(f"{_UNPACKAGED}."):
        return PREDICATES[1]
    if not _clean_subtree(payload, sources, listings, source_dirs, index):
        return PREDICATES[2]
    if not _imports_resolve(sources, listings, source_dirs, index):
        return PREDICATES[3]
    if target is None:
        return PREDICATES[4]
    if confidence < payload.min_extraction_confidence:
        return PREDICATES[5]
    return None


def _all_source_directories(
    carriers: Mapping[str, Sequence[_Carrier]],
) -> Mapping[str, frozenset[str]]:
    """repo_id → every path that is SOME contract's source. The clean-subtree test is about what
    else lives in the directory, and another contract's `.proto` beside this one is not an
    implementation file."""
    out: dict[str, set[str]] = {}
    for rows in carriers.values():
        for carrier in rows:
            out.setdefault(carrier.repo_id, set()).add(carrier.path)
    return {repo_id: frozenset(paths) for repo_id, paths in sorted(out.items())}


def _clean_subtree(
    payload: ContractsInput,
    sources: Sequence[_Carrier],
    listings: Mapping[str, Sequence[str]],
    source_dirs: Mapping[str, frozenset[str]],
    index: _Index,
) -> bool:
    """§3.1 5b (vi): the directories holding the sources hold only contract sources and generated
    output. A `.proto` sitting beside `Service.java` is not extractable, because the hoist would
    have to split a directory — and a half-moved directory is a broken build in two repos.

    Only files sitting DIRECTLY in the directory count: a subdirectory is its own package and
    moves (or does not) on its own evidence.
    """
    for carrier in sources:
        prefix = f"{carrier.directory}/" if carrier.directory else ""
        contracts = source_dirs.get(carrier.repo_id, frozenset())
        for path in listings.get(carrier.repo_id, ()):
            if not path.startswith(prefix) or "/" in path[len(prefix) :]:
                continue
            if path not in contracts and not _is_generated(payload, index, carrier.repo_id, path):
                return False
    return True


def _imports_resolve(
    sources: Sequence[_Carrier],
    listings: Mapping[str, Sequence[str]],
    source_dirs: Mapping[str, frozenset[str]],
    index: _Index,
) -> bool:
    """§3.1 5b (vi): the contract's own imports resolve to other contracts, or to nothing.

    "To nothing" is a pass, not a failure: a `google/protobuf/timestamp.proto` import names a file
    no repo in the fleet carries, and refusing to hoist over it would reject nearly every real
    proto package. What fails is an import that resolves to a file inside the fleet which is NOT a
    contract source — that is an IDL reaching into implementation code, and the hoist would leave
    the reference dangling.
    """
    fleet_paths = {
        (repo_id, path) for repo_id, paths in listings.items() for path in paths
    }
    for carrier in sources:
        for imported in index.imports.get((carrier.repo_id, carrier.path), ()):
            hits = sorted(
                (repo, path)
                for repo, path in fleet_paths
                if path == imported or path.endswith(f"/{imported}")
            )
            for repo, path in hits:
                if path not in source_dirs.get(repo, frozenset()):
                    return False
    return True


# ---------------------------------------------------------------------------------------
# step 8's CONTRACT detector
# ---------------------------------------------------------------------------------------


def _claims(
    payload: ContractsInput, node: ContractNode, sources: Sequence[_Carrier]
) -> list[ContractClaim]:
    """§3.1 step 8's `CONTRACT` rows: one claim per carrier, resolved by 5b (iv)'s own ladder.

    `audit_collisions` emits a row only when two or more repos carry the same `(kind, identifier)`
    — which is exactly the contest the table describes — so a single-carrier contract writes
    nothing, and the detector, not this module, decides `severity`.
    """
    return [
        ContractClaim(
            contract_id=node.contract_id,
            repo_id=carrier.repo_id,
            blob_sha=_blob_sha(payload, carrier),
            vendored=carrier.vendored,
            owner=carrier.repo_id == node.owning_repo_id,
        )
        for carrier in sources
    ]


def _owns_hints(payload: ContractsInput, known: set[str]) -> Mapping[str, str]:
    """`contract_id` → the repo an operator declared owns it, for the collision resolution text."""
    return {
        contract_id: repo.repo_id
        for repo in sorted(payload.repos, key=lambda r: r.repo_id)
        for contract_id in sorted(repo.owns)
        if contract_id in known
    }


# ---------------------------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------------------------

_NO_FACTS: Final = RepoFacts(repo_id="unknown")


def _blob_sha(payload: ContractsInput, carrier: _Carrier) -> str:
    return payload.blob_shas.get(f"{carrier.repo_id}\x00{carrier.path}", "")


def _product(factors: Mapping[str, float]) -> float:
    out = 1.0
    for value in factors.values():
        out *= value
    return out
