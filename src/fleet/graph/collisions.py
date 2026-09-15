"""The coordinate / contract / dest-path / file-path / dep-version audit (§3.1 step 8).

Runs after waves are assigned and **before** any transformation, because a collision discovered
in Phase 3 has already been merged into history. One pass, five detectors, all writing
`CollisionFinding` rows; `fleet sequence` exits non-zero if any row has `severity='error'` and a
`NULL` `resolution`, which `CollisionReport.blocking` answers directly.

Neither `FILE_PATH` nor `DEST_PATH` detection reads a file: both join the relocation path maps
against the `ls-tree` blob SHAs already captured at preflight (§3.1 step 1), which is why every
input here is a plain claim record and this module opens nothing.

`_scc` is reserved (§3.3): a repo `dest` under a `_scc/` segment is refused, so a real repo can
never occupy the namespace the `ATOMIC_WAVE` coarsening emits its union target into.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from fleet.graph.cycles import RESERVED_SCC_SEGMENT, GraphFinding
from fleet.models.enums import Ecosystem, NodeKind
from fleet.models.graph import CollisionFinding

__all__ = [
    "WARN_DIVERGENT_BASENAMES",
    "CollisionInput",
    "CollisionReport",
    "ContractClaim",
    "CoordinateClaim",
    "DestClaim",
    "FileClaim",
    "VersionRequirement",
    "audit_collisions",
    "ownership_rank",
]

WARN_DIVERGENT_BASENAMES: Final[tuple[str, ...]] = (
    ".gitignore", ".gitattributes", ".editorconfig", ".dockerignore",
)
"""§3.1 step 8 `FILE_PATH`: "`severity='error'` under any `src/` root, `warn` for licences, CI
config, and `.gitignore`". Licences and CI config are matched by pattern below; these are the
exact-name remainder."""

type Severity = Literal["warn", "error"]

_WARN_NAME = re.compile(r"^(licen[sc]e|copying|notice|authors)(\.[a-z]+)?$", re.IGNORECASE)
_WARN_DIR = re.compile(r"(^|/)(\.github|\.circleci|\.gitlab|ci|\.buildkite)(/|$)")
_VERSION_ATOM = re.compile(r"^(==|>=|<=|~=|=|>|<|\^|~>|~)?\s*v?(\d+(?:\.\d+)*)")


# =======================================================================================
# inputs
# =======================================================================================


@dataclass(frozen=True, slots=True)
class CoordinateClaim:
    """One repo publishing one `Coordinate.key` (§3.1 step 3).

    `depth` and `commit_count` are the ownership ladder's rungs (ii) and (iii), carried on the
    claim because the detector is a pure function and cannot go and look them up. Both default
    to the value that makes the rung inert, so a caller that has neither still gets rung (iv)'s
    total order on `repo_id` rather than an exception.
    """

    coord_key: str
    repo_id: str
    ecosystem: Ecosystem
    depth: int = 0
    """Path depth of the manifest declaring the publish (`path.count("/")`); SHALLOWEST wins."""
    commit_count: int = 0
    """`repos.commit_count`; HIGHEST wins."""


@dataclass(frozen=True, slots=True)
class ContractClaim:
    """One repo carrying one `(kind, identifier)` contract (§3.1 5b iii/iv)."""

    contract_id: str
    repo_id: str
    blob_sha: str = ""
    vendored: bool = False
    owner: bool = False


@dataclass(frozen=True, slots=True)
class DestClaim:
    """One node's `layout()` destination. `explicit` marks a `dest:` override from
    `config/repos.yaml` — the operator asked for it, so an impossible one is an error."""

    node_id: str
    dest: str
    node_kind: NodeKind = NodeKind.REPO
    explicit: bool = False
    hoisted_contract: bool = False
    inbound_edges: int = 0


@dataclass(frozen=True, slots=True)
class FileClaim:
    """One file landing on one monorepo path after relocation, with the blob SHA preflight
    already read from `ls-tree`."""

    path: str
    repo_id: str
    blob_sha: str
    wave_index: int = 0
    contract_id: str | None = None
    hoisted_contract: bool = False


@dataclass(frozen=True, slots=True)
class VersionRequirement:
    """One repo's requirement on one **external** coordinate."""

    coord_key: str
    repo_id: str
    version_spec: str


@dataclass(frozen=True, slots=True)
class CollisionInput:
    coordinates: Sequence[CoordinateClaim] = ()
    contracts: Sequence[ContractClaim] = ()
    dests: Sequence[DestClaim] = ()
    files: Sequence[FileClaim] = ()
    versions: Sequence[VersionRequirement] = ()
    owns_hints: Mapping[str, str] = field(default_factory=dict)
    """`coord_key` / `contract_id` → the repo the operator declared owns it (§3.1 3, 5b iv)."""
    vendor_repo_ids: Collection[str] = ()
    """Carriers whose copy sits under `scan.vendor_globs`; only non-vendored divergent carriers
    escalate a `CONTRACT` collision to `error`."""


# =======================================================================================
# output
# =======================================================================================


@dataclass(frozen=True, slots=True)
class CollisionReport:
    collisions: tuple[CollisionFinding, ...]
    findings: tuple[GraphFinding, ...]
    dest_rewrites: Mapping[str, str]
    """node_id → the destination it must use instead; the loser of a `DEST_PATH` contest or a
    repo evicted from the reserved `_scc/` namespace."""
    dropped_paths: Mapping[str, tuple[str, ...]]
    """repo_id → monorepo paths its relocation plan must drop (a `HOISTED` contract claimed
    them). This is how the owner's and every vendorer's copy stop being copied at all (§3.3)."""

    @property
    def blocking(self) -> tuple[CollisionFinding, ...]:
        """`severity='error'` with a `NULL` resolution — what makes `fleet sequence` exit
        non-zero (§3.1 step 8). An error WITH a resolution is a recorded decision, not a block."""
        return tuple(c for c in self.collisions if c.severity == "error" and c.resolution is None)

    @property
    def ok(self) -> bool:
        return not self.blocking


def ownership_rank(
    *, publishes_coordinate: bool, depth: int, commit_count: int, repo_id: str
) -> tuple[bool, int, int, str]:
    """The §3.1 (iv) / §13 row 6 ownership ladder as a sort key, where **lowest wins**.

    Four rungs, in order: a repo that publishes a coordinate outranks one that does not; then the
    SHALLOWEST declaring path; then the HIGHEST `commit_count`; then `repo_id` — total, so the
    winner is reproducible rather than dependent on row order.

    It is a function rather than a comment because `COORDINATE` and `CONTRACT` ownership are the
    same question asked about two key spaces, and two ladders that drift apart are two different
    answers to "who owns this" — a disagreement that would surface only as a mis-migrated repo.
    `workers/contracts.py::_owner` ranks `CONTRACT` carriers through this same function, so the
    `CONTRACT` and `COORDINATE` ladders cannot drift apart by an edit to one of them.
    """
    return (not publishes_coordinate, depth, -commit_count, repo_id)


def audit_collisions(data: CollisionInput) -> CollisionReport:
    """One pass, five detectors, in the order §3.1 step 8 tabulates them."""
    collisions: list[CollisionFinding] = []
    findings: list[GraphFinding] = []
    rewrites: dict[str, str] = {}
    dropped: dict[str, set[str]] = {}

    collisions.extend(_coordinate_collisions(data))
    collisions.extend(_contract_collisions(data))
    collisions.extend(_dest_collisions(data, findings, rewrites))
    collisions.extend(_file_collisions(data, findings, dropped))
    collisions.extend(_version_collisions(data))

    return CollisionReport(
        collisions=tuple(sorted(collisions, key=lambda c: (c.kind, c.key))),
        findings=tuple(sorted(findings, key=lambda f: (f.kind, f.repo_id or "", f.fingerprint))),
        dest_rewrites=dict(sorted(rewrites.items())),
        dropped_paths={k: tuple(sorted(v)) for k, v in sorted(dropped.items())},
    )


# =======================================================================================
# COORDINATE / CONTRACT
# =======================================================================================


def _coordinate_collisions(data: CollisionInput) -> list[CollisionFinding]:
    grouped: dict[str, list[CoordinateClaim]] = {}
    for claim in sorted(data.coordinates, key=lambda c: (c.coord_key, c.repo_id)):
        grouped.setdefault(claim.coord_key, []).append(claim)
    out: list[CollisionFinding] = []
    for coord_key, claims in sorted(grouped.items()):
        repo_ids = sorted({c.repo_id for c in claims})
        if len(repo_ids) < 2:
            continue
        # `error` when the carriers disagree about the ecosystem: `Coordinate.key` embeds the
        # ecosystem (ADR-0017), so this is a normalization bug rather than a fleet fact.
        divergent = len({c.ecosystem for c in claims}) > 1
        # Rung (i) is the operator's `owns:` hint — but only when it names a repo that actually
        # publishes this coordinate. A hint pointing anywhere else does not resolve THIS contest,
        # and crowning a non-claimant would record an owner no manifest backs.
        hinted = data.owns_hints.get(coord_key)
        if hinted is not None and hinted in set(repo_ids):
            winner, rule = hinted, "owns-hint"
        else:
            best = min(
                claims,
                key=lambda c: ownership_rank(
                    # Every claimant in this group publishes the coordinate by construction, so
                    # rung (i)'s publishes-vs-not is constant here and cannot discriminate; it is
                    # passed explicitly rather than dropped so the ladder keeps ONE definition.
                    publishes_coordinate=True,
                    depth=c.depth,
                    commit_count=c.commit_count,
                    repo_id=c.repo_id,
                ),
            )
            winner, rule = best.repo_id, "ladder:depth,commit-count,repo-id"
        out.append(
            CollisionFinding(
                kind="COORDINATE",
                key=coord_key,
                repo_ids=repo_ids,
                severity="error" if divergent else "warn",
                resolution=None if divergent else f"{rule}:{winner}",
            )
        )
    return out


def _contract_collisions(data: CollisionInput) -> list[CollisionFinding]:
    grouped: dict[str, list[ContractClaim]] = {}
    for claim in sorted(data.contracts, key=lambda c: (c.contract_id, c.repo_id)):
        grouped.setdefault(claim.contract_id, []).append(claim)
    out: list[CollisionFinding] = []
    for contract_id, claims in sorted(grouped.items()):
        repo_ids = sorted({c.repo_id for c in claims})
        if len(repo_ids) < 2:
            continue
        shas = sorted({c.blob_sha for c in claims if c.blob_sha})
        identical = len(shas) <= 1
        outside_vendor = [
            c.repo_id
            for c in claims
            if not c.vendored and c.repo_id not in set(data.vendor_repo_ids)
        ]
        # Two repos independently EDITING the same published interface is a fleet fact an
        # operator must see before anything merges; nine byte-identical vendored copies are not.
        severity: Severity = (
            "warn" if identical or len(set(outside_vendor)) < 2 else "error"
        )
        owner = data.owns_hints.get(contract_id) or next(
            (c.repo_id for c in claims if c.owner), repo_ids[0]
        )
        out.append(
            CollisionFinding(
                kind="CONTRACT",
                key=contract_id,
                repo_ids=repo_ids,
                blob_shas=shas,
                severity=severity,
                resolution=f"owner:{owner}; losers become consumers (§3.1 5b iv)",
            )
        )
    return out


# =======================================================================================
# DEST_PATH — including the reserved `_scc/` namespace
# =======================================================================================


def _dest_collisions(
    data: CollisionInput, findings: list[GraphFinding], rewrites: dict[str, str]
) -> list[CollisionFinding]:
    out: list[CollisionFinding] = []
    claims = sorted(data.dests, key=lambda d: (d.dest, d.node_kind.value, d.node_id))

    for claim in claims:
        if claim.node_kind is NodeKind.REPO and _is_reserved(claim.dest):
            # §3.3: `_scc` is reserved for the ATOMIC_WAVE coarsening. Recorded as a finding
            # rather than a `collisions` row because the row's `repo_ids` needs two participants
            # and this contest has exactly one (see SPEC_GAPS).
            rewrites[claim.node_id] = f"{claim.dest.rstrip('/')}-{claim.node_id}"
            findings.append(
                GraphFinding(
                    kind="ReservedDestPath",
                    severity="error",
                    repo_id=claim.node_id,
                    payload={
                        "dest": claim.dest,
                        "reserved": RESERVED_SCC_SEGMENT,
                        "rewritten_to": rewrites[claim.node_id],
                    },
                )
            )

    grouped: dict[str, list[DestClaim]] = {}
    for claim in claims:
        grouped.setdefault(rewrites.get(claim.node_id, claim.dest), []).append(claim)

    for dest, group in sorted(grouped.items()):
        if len(group) < 2:
            continue
        # A hoisted contract ALWAYS keeps the path — it is the shared artifact and it migrated
        # first; otherwise the node with more inbound edges keeps it, ties by node_id.
        winner = min(
            group,
            key=lambda c: (0 if c.hoisted_contract else 1, -c.inbound_edges, c.node_id),
        )
        losers = [c for c in group if c.node_id != winner.node_id]
        explicit = any(c.explicit for c in group)
        for loser in losers:
            rewrites[loser.node_id] = f"{dest.rstrip('/')}-{loser.node_id}"
            findings.append(
                GraphFinding(
                    kind="DestPathRewritten",
                    severity="error" if explicit else "warn",
                    repo_id=loser.node_id if loser.node_kind is NodeKind.REPO else None,
                    payload={
                        "dest": dest,
                        "winner": winner.node_id,
                        "rewritten_to": rewrites[loser.node_id],
                    },
                )
            )
        repo_ids = sorted({c.node_id for c in group if c.node_kind is NodeKind.REPO})
        if len(repo_ids) < 2:
            continue  # a repo/contract contest cannot fill `repo_ids` (min_length=2)
        out.append(
            CollisionFinding(
                kind="DEST_PATH",
                key=dest,
                repo_ids=repo_ids,
                severity="error" if explicit else "warn",
                resolution=(
                    f"keep:{winner.node_id}; "
                    + ", ".join(f"{c.node_id}->{rewrites[c.node_id]}" for c in losers)
                ),
            )
        )
    return out


def _is_reserved(dest: str) -> bool:
    return RESERVED_SCC_SEGMENT in [seg for seg in dest.split("/") if seg]


# =======================================================================================
# FILE_PATH
# =======================================================================================


def _file_collisions(
    data: CollisionInput, findings: list[GraphFinding], dropped: dict[str, set[str]]
) -> list[CollisionFinding]:
    grouped: dict[str, list[FileClaim]] = {}
    for claim in sorted(data.files, key=lambda f: (f.path, f.repo_id, f.blob_sha)):
        grouped.setdefault(claim.path, []).append(claim)

    out: list[CollisionFinding] = []
    for path, group in sorted(grouped.items()):
        if len({(c.repo_id, c.contract_id) for c in group}) < 2:
            continue
        repo_ids = sorted({c.repo_id for c in group})
        shas = sorted({c.blob_sha for c in group if c.blob_sha})
        hoisted = next((c for c in group if c.hoisted_contract and c.contract_id), None)
        severity: Severity
        if hoisted is not None:
            for claim in group:
                if claim is not hoisted:
                    dropped.setdefault(claim.repo_id, set()).add(path)
            resolution = f"hoisted:{hoisted.contract_id}"
            severity = "warn"
        elif len(shas) <= 1:
            resolution = "dedupe:identical-blob; written once, both targets reference it"
            severity = "warn"
        else:
            later = max(group, key=lambda c: (c.wave_index, c.repo_id))
            suffixed = f"{path}.{later.repo_id}"
            severity = _divergent_severity(path)
            resolution = f"suffix:{later.repo_id}->{suffixed}"
            findings.append(
                GraphFinding(
                    kind="DuplicateDivergent",
                    severity=severity,
                    repo_id=later.repo_id,
                    payload={"path": path, "rewritten_to": suffixed},
                )
            )
        if len(repo_ids) < 2:
            continue  # contract-vs-owner on one repo cannot fill `repo_ids` (min_length=2)
        out.append(
            CollisionFinding(
                kind="FILE_PATH",
                key=path,
                repo_ids=repo_ids,
                blob_shas=shas,
                severity=severity,
                resolution=resolution,
            )
        )
    return out


def _divergent_severity(path: str) -> Severity:
    """§3.1 step 8: `error` under any `src/` root, `warn` for licences, CI config and
    `.gitignore`. Anything else defaults to `error` — a divergent duplicate is a real
    disagreement, and Rule 11 says fail loud rather than quietly pick one."""
    segments = [s for s in path.split("/") if s]
    basename = segments[-1] if segments else path
    if "src" in segments:
        return "error"
    if basename in WARN_DIVERGENT_BASENAMES:
        return "warn"
    if _WARN_NAME.match(basename) or _WARN_DIR.search(path):
        return "warn"
    return "error"


# =======================================================================================
# DEP_VERSION
# =======================================================================================


def _version_collisions(data: CollisionInput) -> list[CollisionFinding]:
    grouped: dict[str, list[VersionRequirement]] = {}
    for req in sorted(data.versions, key=lambda r: (r.coord_key, r.repo_id, r.version_spec)):
        grouped.setdefault(req.coord_key, []).append(req)

    out: list[CollisionFinding] = []
    for coord_key, reqs in sorted(grouped.items()):
        repo_ids = sorted({r.repo_id for r in reqs})
        specs = sorted({r.version_spec for r in reqs})
        if len(repo_ids) < 2 or len(specs) < 2:
            continue
        satisfiable = _intersects(specs)
        # Bazel's MVS selects the minimum version satisfying ALL specs; a plurality vote is never
        # taken, because it ships a build violating a declared upper bound. Only an EMPTY
        # intersection is a conflict, and only then is the `conflict_resolution` role invoked.
        out.append(
            CollisionFinding(
                kind="DEP_VERSION",
                key=coord_key,
                repo_ids=repo_ids,
                severity="warn" if satisfiable else "error",
                resolution=(
                    "mvs:bazel_dep selects the minimum version satisfying every spec"
                    if satisfiable
                    else None
                ),
            )
        )
    return out


type _Bound = tuple[tuple[int, ...], bool]


def _intersects(specs: Iterable[str]) -> bool:
    """True unless the specs' intersection is provably empty. Unparseable specs are ignored
    rather than guessed at: a `VersionConflict` at `severity='error'` blocks the run, so it must
    never rest on a parse this module was not sure of."""
    lo: _Bound | None = None
    hi: _Bound | None = None
    for spec in specs:
        parsed = _bounds(spec)
        if parsed is None:
            continue
        low, high = parsed
        if low is not None and (lo is None or (low[0], not low[1]) > (lo[0], not lo[1])):
            lo = low
        if high is not None and (hi is None or (high[0], high[1]) < (hi[0], hi[1])):
            hi = high
    if lo is None or hi is None:
        return True
    if lo[0] > hi[0]:
        return False
    return not (lo[0] == hi[0] and not (lo[1] and hi[1]))


def _bounds(spec: str) -> tuple[_Bound | None, _Bound | None] | None:
    """One spec → (lower, upper) inclusive-flagged bounds, or None when unparseable."""
    low: _Bound | None = None
    high: _Bound | None = None
    atoms = [a.strip() for a in re.split(r"[,\s]+", spec.strip()) if a.strip()]
    if not atoms:
        return None
    for atom in atoms:
        match = _VERSION_ATOM.fullmatch(atom)
        if match is None:
            return None
        op = match.group(1) or "=="
        version, precision = _parse(match.group(2))
        if op in ("==", "="):
            low, high = (version, True), (version, True)
        elif op == ">=":
            low = (version, True)
        elif op == ">":
            low = (version, False)
        elif op == "<=":
            high = (version, True)
        elif op == "<":
            high = (version, False)
        elif op == "^":
            low, high = (version, True), (_caret_ceiling(version), False)
        elif op == "~=":
            # PEP 440 compatible-release: `~=V.N` means `>=V.N, ==V.*` — every specified
            # component except the last is locked, the last is free to increase.
            low, high = (version, True), (_compatible_release_ceiling(version, precision), False)
        else:  # ~ / ~> (this module's own tilde convention, distinct from PEP 440's `~=`)
            low, high = (version, True), (_tilde_ceiling(version, precision), False)
    return low, high


def _parse(text: str) -> tuple[tuple[int, ...], int]:
    """A version string → its (3-component-padded) parts, alongside `precision`: how many
    components the string ACTUALLY specified (capped at 3), before padding. Padding alone loses
    that distinction — `~1` and `~1.0.0` both pad to `(1, 0, 0)` — and the tilde-family operators
    below need it to know which component is free to increase."""
    parts = tuple(int(p) for p in text.split("."))
    precision = min(len(parts), 3)
    padded = parts + (0,) * (3 - len(parts)) if len(parts) < 3 else parts
    return padded, precision


def _caret_ceiling(version: tuple[int, ...]) -> tuple[int, ...]:
    major, minor, *_ = version
    if major:
        return (major + 1, 0, 0)
    if minor:
        return (0, minor + 1, 0)
    return (0, 0, version[2] + 1)


def _widen_ceiling(
    version: tuple[int, ...], precision: int, *, floor_precision: int
) -> tuple[int, ...]:
    """Shared shape for the tilde-family ceilings: widen the minor component, unless the spec
    named too few components to have a minor to lock (`precision <= floor_precision`), in which
    case there is nothing to lock there and the major widens instead."""
    major, minor, *_ = version
    if precision <= floor_precision:
        return (major + 1, 0, 0)
    return (major, minor + 1, 0)


def _tilde_ceiling(version: tuple[int, ...], precision: int) -> tuple[int, ...]:
    """This module's own `~` / `~>` convention. A bare major-only spec (`~1`) has no minor
    component to lock, so the major widens instead (`<2.0`); `~1.5`-shaped specs are unaffected
    (`<1.6`, as before)."""
    return _widen_ceiling(version, precision, floor_precision=1)


def _compatible_release_ceiling(version: tuple[int, ...], precision: int) -> tuple[int, ...]:
    """PEP 440 `~=`: `~=2.28` (2 components) allows `2.x`, ceiling `3.0`; `~=2.28.1` (3
    components) allows `2.28.x`, ceiling `2.29.0`."""
    return _widen_ceiling(version, precision, floor_precision=2)
