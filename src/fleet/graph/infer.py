"""The `EdgeKind` inference rules and the confidence score (§3.1 step 5, §3.1 5b vii).

The score must be reconstructible from `DependencyEdge.confidence_factors` alone: every
applied modifier is recorded, so a reviewer can audit why an edge did or did not order
migration. Sub-`min_confidence` edges are reported, never used for ordering.

**Orientation.** Every edge produced here is directed **dependent → dependency**: `src`
requires `dst`. Nothing in this module ever emits the reverse; `graph/build.py` consumes the
rows exactly as written. See the module docstring of `graph/build.py` for why that sentence is
load-bearing.

**Determinism.** Inference is a pure function of its input value objects (§11.6): no `hash()`,
no set-iteration order reaching the output, no timestamp in any key. Every intermediate index
is a `dict` built from `sorted()` input and every fan-out loop walks a sorted sequence, so two
processes under different `PYTHONHASHSEED` emit byte-identical edges in the same order.
`DependencyEdge.detected_at` is the one wall-clock field, and it is excluded from `edge_key`.

**`edge_key_for` is re-exported, not defined here** (§5 `models/graph.py`). Inference used to
carry its own copy of the recipe and §6's DDL a third; one key with three definitions is the
defect ADR-0026 exists to prevent, so the model that declares the field owns the derivation and
everything else calls it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Final

from fleet.models.enums import Ecosystem, EdgeKind, NodeKind, SymbolKind
from fleet.models.graph import (
    NO_LINE,
    ContractNode,
    DependencyEdge,
    SymbolRef,
    edge_key_for,
)
from fleet.models.repo import Coordinate, ManifestRef, RawDependency
from fleet.settings import ScanSection

__all__ = [
    "API_SYMBOL_KINDS",
    "EDGE_BASE_CONFIDENCE",
    "MODIFIERS",
    "NO_LINE",
    "RESOURCE_SYMBOL_KINDS",
    "SYMBOL_DERIVED_KINDS",
    "TEST_SCOPES",
    "TRUNCATED_INDEX_CAP",
    "InferenceInput",
    "ManifestDependency",
    "OwnerIndex",
    "edge_key_for",
    "infer_contract_edges",
    "infer_edges",
]

#: §3.1 step 5 and 5b (vii): the fixed base confidence of each `EdgeKind`, pre-modifier.
EDGE_BASE_CONFIDENCE: Final[dict[EdgeKind, float]] = {
    EdgeKind.DECLARED_DEP: 1.0,
    EdgeKind.PUBLISHED_ARTIFACT: 0.95,
    EdgeKind.INTERNAL_IMPORT: 0.8,
    EdgeKind.API_CONTRACT: 0.7,
    EdgeKind.SHARED_RESOURCE: 0.5,
    EdgeKind.DYNAMIC_REF: 0.3,
    EdgeKind.CONTRACT_IMPL: 1.0,
    EdgeKind.CONTRACT_CONSUME: 0.85,
}

#: §3.1 step 5, the modifier table. The dict key is what lands in `confidence_factors`.
MODIFIERS: Final[dict[str, float]] = {
    "open_range": 0.9,
    "ambiguous": 0.8,
    "vendored": 0.4,
    "generated": 0.6,
    "test_scope": 0.7,
    "llm_extracted": 0.8,
}

TRUNCATED_INDEX_CAP: Final = 0.6
"""§3.1 step 4: a repo past `scan.max_symbols_per_repo` "continues with a partial index whose
edges are capped at confidence 0.6". Applied to the SYMBOL-DERIVED kinds only: a manifest join
is not read out of the symbol index, so a truncated index is no evidence against it."""

TEST_SCOPES: Final[frozenset[str]] = frozenset(
    {
        "test",
        "dev",
        "devDependencies",
        "optional",
        # Gradle's `RawDependency.scope` is the configuration name "as written" (its own
        # docstring), not a normalized token — `manifests/gradle.py`'s `_CONFIGURATIONS` names
        # these five as its test-related configurations, none of which equal the literal "test".
        "testAnnotationProcessor",
        "testCompile",
        "testCompileOnly",
        "testImplementation",
        "testRuntimeOnly",
    }
)

API_SYMBOL_KINDS: Final[frozenset[SymbolKind]] = frozenset(
    {SymbolKind.GRPC_SERVICE, SymbolKind.PROTO_MESSAGE, SymbolKind.HTTP_OPERATION}
)
RESOURCE_SYMBOL_KINDS: Final[frozenset[SymbolKind]] = frozenset(
    {SymbolKind.DB_TABLE, SymbolKind.QUEUE_TOPIC}
)
SYMBOL_DERIVED_KINDS: Final[frozenset[EdgeKind]] = frozenset(
    {EdgeKind.INTERNAL_IMPORT, EdgeKind.API_CONTRACT, EdgeKind.SHARED_RESOURCE,
     EdgeKind.DYNAMIC_REF}
)

_SCAN: Final = ScanSection()  # the one source of the default vendor/generated globs (§9)

# A version spec that names one release and nothing else. Everything else — `^1.2`, `~1`, `1.+`,
# `latest`, `*`, `workspace:*`, `file:../x`, absent — is an open range and costs ×0.9.
_PINNED_RE: Final = re.compile(r"[vV]?\d+(?:\.\d+)*(?:[.+-][0-9A-Za-z.+-]+)?")
# Splitting on every non-alphanumeric run makes coordinate and import matching ecosystem-neutral:
# `com.acme:commons`, `@acme/ui` and `github.com/acme/svc` all reduce to a token list, and so does
# the import that references them. Per-language knowledge stays in `ManifestAdapter` (ADR-0005).
_TOKEN_SPLIT: Final = re.compile(r"[^0-9a-z]+")


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(t for t in _TOKEN_SPLIT.split(text.lower()) if t)


@lru_cache(maxsize=64)
def _glob_regex(globs: tuple[str, ...]) -> re.Pattern[str] | None:
    """`**/` = any directory prefix, `**` = anything, `*` = anything but a separator."""
    if not globs:
        return None
    return re.compile("|".join(f"(?:{_glob_to_regex(g)})" for g in globs))


def _glob_to_regex(pattern: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return "".join(out)


def _matches(path: str, globs: tuple[str, ...]) -> bool:
    rx = _glob_regex(globs)
    return rx is not None and rx.fullmatch(path) is not None


def _is_pinned(spec: str | None) -> bool:
    """True when the spec names exactly one published release (Maven's `[1.2.0]` included)."""
    if spec is None:
        return False
    text = spec.strip()
    if text.startswith("[") and text.endswith("]") and "," not in text:
        text = text[1:-1].strip()
    return bool(text) and _PINNED_RE.fullmatch(text) is not None


# ---------------------------------------------------------------------------------------
# inputs — value objects over rows Phase 1 steps 2-4 already persisted
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ManifestDependency:
    """One declared dependency and the manifest row that proves it (§3.1 steps 2-3)."""

    manifest: ManifestRef
    raw: RawDependency
    coordinate: Coordinate

    @property
    def evidence_line(self) -> int | None:
        return self.raw.source_line


@dataclass(frozen=True, slots=True)
class OwnerIndex:
    """The `coordinates` table as §3.1 step 3 uses it: the internal-vs-external oracle.

    A coordinate with no owner here is external and yields no edge at all. A coordinate with
    more than one owner is `ambiguous`: §3.1 step 5 expands it to one ordering constraint per
    candidate, because "over-ordering is safe and under-ordering is not".
    """

    owners: Mapping[str, tuple[str, ...]]
    """`Coordinate.key` → owning repo_ids, sorted. >1 entry ⇒ ambiguous."""
    published: Mapping[str, Coordinate]
    """repo_id → the coordinate it publishes (lowest `key` when it publishes several)."""
    prefixes: Mapping[tuple[str, ...], Coordinate]
    """Token form of every published coordinate → that coordinate. Drives INTERNAL_IMPORT."""

    @classmethod
    def from_published(cls, published: Iterable[tuple[str, Coordinate]]) -> OwnerIndex:
        rows = sorted({(repo_id, coord) for repo_id, coord in published},
                      key=lambda row: (row[0], row[1].key))
        owners: dict[str, list[str]] = {}
        by_repo: dict[str, Coordinate] = {}
        prefixes: dict[tuple[str, ...], Coordinate] = {}
        for repo_id, coord in rows:
            holders = owners.setdefault(coord.key, [])
            if repo_id not in holders:
                holders.append(repo_id)
            best = by_repo.get(repo_id)
            if best is None or coord.key < best.key:
                by_repo[repo_id] = coord
            tokens = _tokens(coord.group) + _tokens(coord.name)
            if tokens and (prefixes.get(tokens) is None or coord.key < prefixes[tokens].key):
                prefixes[tokens] = coord
        return cls(
            owners={key: tuple(sorted(v)) for key, v in sorted(owners.items())},
            published=dict(sorted(by_repo.items())),
            prefixes=dict(sorted(prefixes.items())),
        )

    def owners_of(self, coord_key: str) -> tuple[str, ...]:
        return self.owners.get(coord_key, ())

    def coordinate_of(self, repo_id: str) -> Coordinate:
        """The dst `Coordinate` every REPO-dst edge must carry. A repo that publishes nothing
        gets the §3.1 step 2 unknown-ecosystem synthetic, which is what keeps an unmanifested
        repo a first-class node instead of an edge the model refuses to construct."""
        known = self.published.get(repo_id)
        if known is not None:
            return known
        return Coordinate(ecosystem=Ecosystem.UNKNOWN, group="", name=repo_id)

    def match_import(self, fqn: str) -> Coordinate | None:
        """Longest published-coordinate token prefix of `fqn`, or None (§3.1 step 5)."""
        tokens = _tokens(fqn)
        for size in range(len(tokens), 0, -1):
            hit = self.prefixes.get(tokens[:size])
            if hit is not None:
                return hit
        return None


@dataclass(frozen=True, slots=True)
class InferenceInput:
    """Everything step 5 reads. Rows only — inference opens no file and calls no model."""

    owners: OwnerIndex
    dependencies: Sequence[ManifestDependency] = ()
    symbols: Sequence[SymbolRef] = ()
    contracts: Sequence[ContractNode] = ()
    truncated_repo_ids: frozenset[str] = field(default_factory=frozenset)
    """Repos that hit `scan.max_symbols_per_repo` — their symbol-derived edges cap at 0.6."""
    vendor_globs: tuple[str, ...] = _SCAN.vendor_globs
    generated_globs: tuple[str, ...] = _SCAN.generated_globs


# ---------------------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------------------


def _score(kind: EdgeKind, factors: dict[str, float], *, truncated: bool) -> tuple[float, float]:
    """`(base, clamp(base × Π modifiers, 0, 1))`, mutating `factors` to stay reconstructible.

    The truncated-index rule is a *cap*, not a multiplier, and a cap cannot be recovered from a
    dict of multipliers. It is therefore stored as the exact ratio that produces the capped
    value, so `base × Π confidence_factors` still reproduces `confidence` to the last bit —
    which is the property §3.1 step 5 demands of the column.
    """
    base = EDGE_BASE_CONFIDENCE[kind]
    product = _product(base, factors)
    if truncated and product > TRUNCATED_INDEX_CAP:
        factors["truncated_index"] = TRUNCATED_INDEX_CAP / product
        product = _product(base, factors)
    return base, min(max(product, 0.0), 1.0)


def _product(base: float, factors: Mapping[str, float]) -> float:
    value = base
    for name in sorted(factors):  # sorted: float multiplication is not associative
        value *= factors[name]
    return value


def _evidence_factors(path: str, inp: InferenceInput) -> dict[str, float]:
    factors: dict[str, float] = {}
    if _matches(path, inp.vendor_globs):
        factors["vendored"] = MODIFIERS["vendored"]
    if _matches(path, inp.generated_globs):
        factors["generated"] = MODIFIERS["generated"]
    return factors


def _repo_edge(
    *,
    src_id: str,
    dst_repo_id: str,
    dst_coordinate: Coordinate,
    kind: EdgeKind,
    evidence_path: str,
    evidence_line: int | None,
    factors: dict[str, float],
    candidates: tuple[str, ...] = (),
    version_spec: str | None = None,
    truncated: bool = False,
) -> DependencyEdge:
    """One repo→repo row, dependent → dependency. `src_id` requires `dst_repo_id`."""
    base, confidence = _score(kind, factors, truncated=truncated)
    return DependencyEdge(
        edge_key=edge_key_for(
            src_kind=NodeKind.REPO,
            src_id=src_id,
            dst_kind=NodeKind.REPO,
            dst_ref=dst_coordinate.key,
            kind=kind,
            evidence_path=evidence_path,
            evidence_line=evidence_line,
        ),
        src_kind=NodeKind.REPO,
        src_id=src_id,
        dst_kind=NodeKind.REPO,
        dst_coordinate=dst_coordinate,
        dst_id=dst_repo_id,
        dst_candidate_repo_ids=list(candidates),
        kind=kind,
        version_spec=version_spec,
        base_confidence=base,
        confidence=confidence,
        confidence_factors=dict(sorted(factors.items())),
        ambiguous=len(candidates) > 1,
        evidence_path=evidence_path,
        evidence_line=evidence_line,
    )


# ---------------------------------------------------------------------------------------
# the rules
# ---------------------------------------------------------------------------------------


def _manifest_edges(inp: InferenceInput) -> list[DependencyEdge]:
    """DECLARED_DEP (1.0) and PUBLISHED_ARTIFACT (0.95) — the indexed join of §3.1 step 5.

    The two kinds are the same join split by one question: does the version spec pin a published
    release? A pin means the dependent compiles against the built artifact; anything looser
    (`^1.2`, `workspace:*`, `file:../x`, absent) means it tracks the source module. *Agent
    recommendation*: §3.1 names the distinction but not the test, and a pin is the only signal
    available from a manifest alone.
    """
    out: list[DependencyEdge] = []
    for dep in sorted(inp.dependencies, key=_dependency_sort_key):
        owners = inp.owners.owners_of(dep.coordinate.key)
        if not owners:
            continue  # external: `dst_id` is None, and an edge that orders nothing is not written
        spec = dep.coordinate.version_spec or dep.raw.version_spec
        pinned = _is_pinned(spec)
        kind = EdgeKind.PUBLISHED_ARTIFACT if pinned else EdgeKind.DECLARED_DEP
        for owner in owners:
            if owner == dep.manifest.repo_id:
                continue  # a repo declaring its own coordinate is not a dependency on itself
            factors = _evidence_factors(dep.manifest.path, inp)
            if not pinned:
                factors["open_range"] = MODIFIERS["open_range"]
            if len(owners) > 1:
                factors["ambiguous"] = MODIFIERS["ambiguous"]
            if (dep.raw.scope or "") in TEST_SCOPES or dep.raw.optional:
                factors["test_scope"] = MODIFIERS["test_scope"]
            if dep.manifest.low_confidence:
                factors["llm_extracted"] = MODIFIERS["llm_extracted"]
            out.append(
                _repo_edge(
                    src_id=dep.manifest.repo_id,
                    dst_repo_id=owner,
                    dst_coordinate=dep.coordinate,
                    kind=kind,
                    evidence_path=dep.manifest.path,
                    evidence_line=dep.evidence_line,
                    factors=factors,
                    candidates=owners,
                    version_spec=spec,
                )
            )
    return out


def _import_edges(inp: InferenceInput) -> list[DependencyEdge]:
    """INTERNAL_IMPORT (0.8) — the undeclared-dependency detector (§3.1 step 5, Constraint 4).

    An import whose module prefix resolves to a coordinate owned by another repo, *with no
    corresponding manifest entry*. The manifest-entry test is what keeps this kind disjoint from
    DECLARED_DEP rather than double-counting every declared dependency.
    """
    declared = {(dep.manifest.repo_id, dep.coordinate.key) for dep in inp.dependencies}
    out: list[DependencyEdge] = []
    for sym in _sorted_symbols(inp.symbols):
        if sym.kind is not SymbolKind.IMPORT or sym.is_definition:
            continue
        coord = inp.owners.match_import(sym.fqn)
        if coord is None or (sym.repo_id, coord.key) in declared:
            continue
        for owner in inp.owners.owners_of(coord.key):
            if owner == sym.repo_id:
                continue
            factors = _evidence_factors(sym.path, inp)
            if len(inp.owners.owners_of(coord.key)) > 1:
                factors["ambiguous"] = MODIFIERS["ambiguous"]
            out.append(
                _repo_edge(
                    src_id=sym.repo_id,
                    dst_repo_id=owner,
                    dst_coordinate=coord,
                    kind=EdgeKind.INTERNAL_IMPORT,
                    evidence_path=sym.path,
                    evidence_line=sym.line,
                    factors=factors,
                    candidates=inp.owners.owners_of(coord.key),
                    truncated=sym.repo_id in inp.truncated_repo_ids,
                )
            )
    return out


def _api_contract_edges(inp: InferenceInput) -> list[DependencyEdge]:
    """API_CONTRACT (0.7) — an FQN defined in B and referenced in A (§3.1 step 5)."""
    definitions: dict[str, list[str]] = {}
    for sym in _sorted_symbols(inp.symbols):
        if sym.kind in API_SYMBOL_KINDS and sym.is_definition:
            holders = definitions.setdefault(sym.fqn, [])
            if sym.repo_id not in holders:
                holders.append(sym.repo_id)
    out: list[DependencyEdge] = []
    for sym in _sorted_symbols(inp.symbols):
        if sym.kind not in API_SYMBOL_KINDS or sym.is_definition:
            continue
        for owner in definitions.get(sym.fqn, []):
            if owner == sym.repo_id:
                continue
            out.append(
                _repo_edge(
                    src_id=sym.repo_id,
                    dst_repo_id=owner,
                    dst_coordinate=inp.owners.coordinate_of(owner),
                    kind=EdgeKind.API_CONTRACT,
                    evidence_path=sym.path,
                    evidence_line=sym.line,
                    factors=_evidence_factors(sym.path, inp),
                    truncated=sym.repo_id in inp.truncated_repo_ids,
                )
            )
    return out


def _shared_resource_edges(inp: InferenceInput) -> list[DependencyEdge]:
    """SHARED_RESOURCE (0.5) — one table/topic/queue named by two repos (§3.1 step 5).

    Advisory and excluded from the DAG by default, because shared infrastructure is not a build
    ordering constraint. The relation is symmetric, so it is written as one row per *ordered*
    pair: every row's `evidence_path` then lies inside its own `src` repo, which is what lets it
    be reported on that repo's PR body.
    """
    holders: dict[str, list[SymbolRef]] = {}
    for sym in _sorted_symbols(inp.symbols):
        if sym.kind in RESOURCE_SYMBOL_KINDS:
            holders.setdefault(sym.fqn, []).append(sym)
    out: list[DependencyEdge] = []
    for _fqn, syms in sorted(holders.items()):
        repos = sorted({s.repo_id for s in syms})
        if len(repos) < 2:
            continue
        for sym in syms:
            for other in repos:
                if other == sym.repo_id:
                    continue
                out.append(
                    _repo_edge(
                        src_id=sym.repo_id,
                        dst_repo_id=other,
                        dst_coordinate=inp.owners.coordinate_of(other),
                        kind=EdgeKind.SHARED_RESOURCE,
                        evidence_path=sym.path,
                        evidence_line=sym.line,
                        factors=_evidence_factors(sym.path, inp),
                        truncated=sym.repo_id in inp.truncated_repo_ids,
                    )
                )
    return out


def _dynamic_ref_edges(inp: InferenceInput) -> list[DependencyEdge]:
    """DYNAMIC_REF (0.3) — a reference the compiler cannot see (§3.1 step 5).

    Advisory and excluded from the DAG by default, but always reported: an unbroken build that
    `NoClassDefFoundError`s at runtime is the failure this row exists to predict.
    """
    defined: dict[str, list[str]] = {}
    for sym in _sorted_symbols(inp.symbols):
        if sym.is_definition:
            holders = defined.setdefault(sym.fqn, [])
            if sym.repo_id not in holders:
                holders.append(sym.repo_id)
    out: list[DependencyEdge] = []
    for sym in _sorted_symbols(inp.symbols):
        if sym.kind is not SymbolKind.DYNAMIC_REF:
            continue
        coord = inp.owners.match_import(sym.fqn)
        targets = inp.owners.owners_of(coord.key) if coord is not None else ()
        if not targets:
            targets = tuple(defined.get(sym.fqn, ()))
        for owner in targets:
            if owner == sym.repo_id:
                continue
            factors = _evidence_factors(sym.path, inp)
            if len(targets) > 1:
                factors["ambiguous"] = MODIFIERS["ambiguous"]
            out.append(
                _repo_edge(
                    src_id=sym.repo_id,
                    dst_repo_id=owner,
                    dst_coordinate=coord if coord is not None
                    else inp.owners.coordinate_of(owner),
                    kind=EdgeKind.DYNAMIC_REF,
                    evidence_path=sym.path,
                    evidence_line=sym.line,
                    factors=factors,
                    candidates=targets if len(targets) > 1 else (),
                    truncated=sym.repo_id in inp.truncated_repo_ids,
                )
            )
    return out


def infer_contract_edges(contracts: Iterable[ContractNode]) -> list[DependencyEdge]:
    """CONTRACT_IMPL (1.0) and CONTRACT_CONSUME (0.85) — §3.1 5b (vii).

    Both point **at** the contract node, never away from it: no row has `src_kind='CONTRACT'`
    with `dst_kind='REPO'`. That is the whole mechanism — a contract is a sink in `G` and a
    source in `G_rev`, so it can always be scheduled first.

    **No confidence modifier is applied here, deliberately.** §3.1 5b (ii) makes generated code
    *evidence of consumption*, and 5b (iii) collapses byte-identical vendored copies "with no
    penalty" — so charging the `generated` or `vendored` modifiers against these two kinds would
    invert the two rules that produced them, and could push a CONTRACT_CONSUME below
    `min_confidence`, deleting an ordering constraint the hoist exists to create.

    The caller decides *which* contracts to materialize: 6c-H's saturating and marginal trials
    are exactly "run this over a subset". Contracts with no `owning_repo_id` emit nothing.
    """
    out: list[DependencyEdge] = []
    for contract in sorted(contracts, key=lambda c: c.contract_id):
        owner = contract.owning_repo_id
        if owner is None:
            continue
        paths_by_repo: dict[str, list[str]] = {}
        for entry in [*contract.source_paths, *contract.generated_paths]:
            repo_id, path = entry.get("repo_id"), entry.get("path")
            if repo_id and path:
                paths_by_repo.setdefault(repo_id, []).append(path)
        owner_paths = sorted(set(paths_by_repo.get(owner, ())))
        if owner_paths:
            out.append(
                _contract_edge(
                    src_id=owner,
                    contract_id=contract.contract_id,
                    kind=EdgeKind.CONTRACT_IMPL,
                    evidence_path=owner_paths[0],
                )
            )
        for consumer in sorted(set(contract.consumer_repo_ids)):
            if consumer == owner:
                continue
            for path in sorted(set(paths_by_repo.get(consumer, ()))):
                out.append(
                    _contract_edge(
                        src_id=consumer,
                        contract_id=contract.contract_id,
                        kind=EdgeKind.CONTRACT_CONSUME,
                        evidence_path=path,
                    )
                )
    return out


def _contract_edge(
    *, src_id: str, contract_id: str, kind: EdgeKind, evidence_path: str
) -> DependencyEdge:
    base = EDGE_BASE_CONFIDENCE[kind]
    return DependencyEdge(
        edge_key=edge_key_for(
            src_kind=NodeKind.REPO,
            src_id=src_id,
            dst_kind=NodeKind.CONTRACT,
            dst_ref=contract_id,
            kind=kind,
            evidence_path=evidence_path,
            evidence_line=None,
        ),
        src_kind=NodeKind.REPO,
        src_id=src_id,
        dst_kind=NodeKind.CONTRACT,
        dst_id=contract_id,
        kind=kind,
        base_confidence=base,
        confidence=base,
        evidence_path=evidence_path,
    )


# ---------------------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------------------


def infer_edges(inp: InferenceInput) -> tuple[DependencyEdge, ...]:
    """Every edge §3.1 step 5 (and 5b vii) infers, deduplicated by `edge_key` and totally
    ordered by the semantic tuple — so the same rows come back in the same order every run."""
    found = [
        *_manifest_edges(inp),
        *_import_edges(inp),
        *_api_contract_edges(inp),
        *_shared_resource_edges(inp),
        *_dynamic_ref_edges(inp),
        *infer_contract_edges(inp.contracts),
    ]
    seen: set[str] = set()
    unique: list[DependencyEdge] = []
    for edge in sorted(found, key=_edge_sort_key):
        if edge.edge_key in seen:
            continue
        seen.add(edge.edge_key)
        unique.append(edge)
    return tuple(unique)


def _edge_sort_key(edge: DependencyEdge) -> tuple[str, str, str, str, str, str, int, str]:
    return (
        edge.src_kind.value,
        edge.src_id,
        edge.dst_kind.value,
        edge.dst_id or "",
        edge.kind.value,
        edge.evidence_path,
        NO_LINE if edge.evidence_line is None else edge.evidence_line,
        edge.edge_key,
    )


def _dependency_sort_key(dep: ManifestDependency) -> tuple[str, str, str, int, str]:
    return (
        dep.manifest.repo_id,
        dep.manifest.path,
        dep.coordinate.key,
        NO_LINE if dep.evidence_line is None else dep.evidence_line,
        dep.raw.raw_id,
    )


def _sorted_symbols(symbols: Sequence[SymbolRef]) -> list[SymbolRef]:
    return sorted(symbols, key=lambda s: (s.repo_id, s.path, s.line, s.fqn, s.kind.value))
