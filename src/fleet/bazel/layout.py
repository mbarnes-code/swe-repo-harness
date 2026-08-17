"""Monorepo destination paths for every graph node (SPEC §3.3, ADR-0007/ADR-0020).

`layout()` is **total over graph nodes, not just repos**, and it is total *because the adapter
registry is total* (§1) — not because this module carries a fallback branch. Three consequences
are load bearing, and each is the shape of a defect that was found in review:

* **No ecosystem knowledge lives here.** `monorepo_dir` and `path_tail` are read off the
  registered `EcosystemAdapter`; §13 row 33 requires that re-pointing `NPM`'s `monorepo_dir`
  moves every `dest`, `BuildTarget.package` and `//` label with **zero** edits outside
  `src/fleet/ecosystems/js.py`. So this module names no directory and compares no `Ecosystem`
  member — the registry arrives as a `Protocol` parameter (CLAUDE.md guardrail 3), which is also
  what makes the whole module testable with a five-line fake and no adapter package installed.
* **A node with no primary published coordinate is not a special case.** It resolves through the
  same registry lookup against a coordinate synthesized from its own `node_id`, which is how
  §3.1 step 2's unknown-ecosystem path reaches `<misc>/<repo_id>` without an `if` here.
* **`_scc` is reserved under every `monorepo_dir`.** The Phase 1 step-8 audit
  (`graph/collisions.py`) is where a repo landing there becomes a recorded `FILE_PATH`
  collision; this module refuses to *return* such a dest at all, because by Phase 3 the tree has
  already been merged and a repo sitting in the coarsening namespace would silently shadow the
  union target that `ATOMIC_WAVE` emits there (§3.1 6e).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar, Final, Protocol

from fleet.graph.cycles import RESERVED_SCC_SEGMENT, scc_dest, scc_target_name
from fleet.models.enums import Ecosystem
from fleet.models.repo import Coordinate

__all__ = [
    "MODULE_FILES",
    "RESERVED_SCC_SEGMENT",
    "SKELETON_DIRS",
    "STUB_ROOT",
    "EcosystemRegistry",
    "LayoutAdapter",
    "LayoutNode",
    "PublishedCoordinate",
    "ReservedDestError",
    "is_reserved_dest",
    "layout",
    "normalize_dest",
    "scc_label",
    "scc_package",
    "select_primary_coordinate",
    "skeleton_paths",
    "stub_dest",
]


MODULE_FILES: Final[tuple[str, ...]] = ("MODULE.bazel", ".bazelrc", ".bazelversion")
"""The pipeline-owned files at the monorepo root (§3.3 "Target monorepo layout")."""

SKELETON_DIRS: Final[tuple[str, ...]] = ("tools", "third_party", "third_party/stubs")
"""The fixed skeleton. Every *language* directory below the root is `adapter.monorepo_dir` and
is deliberately absent from this tuple — it is not a constant in the spec, and hardcoding it is
exactly what §13 row 33 greps for."""

STUB_ROOT: Final = "third_party/stubs"
"""§3.5: generated stubs for abandoned deps; read by `DEGRADED` consumers only."""


class LayoutAdapter(Protocol):
    """The slice of `EcosystemAdapter` (§7.5) that layout needs, and nothing more.

    A `Protocol` rather than an import of the ABC so this module has no dependency on the
    adapter package: `bazel/` is a driver, and a driver that imports its plugins is not one.
    """

    monorepo_dir: ClassVar[str]
    """The single root this adapter owns, e.g. the JVM adapter's. Never named here.

    Declared `ClassVar`, not `@property`: every real `EcosystemAdapter` (`jvm.py`, `py.py`,
    `rust.py`, `go.py`, `js.py`, `unknown.py`) defines this as a plain `ClassVar[str]` class
    constant, never a computed property — `path_tail` below is the one that's actually derived
    per-call. A `@property` declaration here was structurally looser than the real contract, and
    Pyright's protocol-conformance check (unlike mypy's) treats a directly-declared `ClassVar`
    member as failing to satisfy a `@property` protocol member ("monorepo_dir is not defined as a
    ClassVar in protocol") — confirmed by isolated repro to trigger specifically when the checked
    class declares the attribute as `ClassVar` at that level (inherited-and-reassigned members
    don't trigger it), independent of venv import resolution. Declaring it `ClassVar` here matches
    the real shape and satisfies both checkers."""

    def path_tail(self, coordinate: Coordinate) -> str:
        """The dest path below `monorepo_dir`. Pure and deterministic (§7.5)."""


class EcosystemRegistry(Protocol):
    """`ecosystems.for_ecosystem` — total after `discover()` (§7.5). Positional-only so the
    module-level function satisfies the protocol as written, with no adapter object in between."""

    def for_ecosystem(self, ecosystem: Ecosystem, /) -> LayoutAdapter: ...


class ReservedDestError(ValueError):
    """A node's destination fell inside the `_scc/` coarsening namespace (§3.3).

    Raised rather than repaired: the repair is `CollisionReport.dest_rewrites`, decided in Phase 1
    by the step-8 audit's policy table, and inventing a different one in Phase 3 would mean two
    modules disagreeing about where a repo's history was merged.
    """

    def __init__(self, node_id: str, dest: str) -> None:
        self.node_id = node_id
        self.dest = dest
        super().__init__(
            f"{node_id}: dest {dest!r} lies under the reserved {RESERVED_SCC_SEGMENT!r} "
            f"segment; §3.1 step 8 must rewrite it first"
        )


@dataclass(frozen=True, slots=True)
class PublishedCoordinate:
    """One coordinate a node publishes, with the inbound-edge count that ranks it."""

    coordinate: Coordinate
    inbound_edges: int = 0


@dataclass(frozen=True, slots=True)
class LayoutNode:
    """A graph node reduced to what decides its destination — a repo *or* a hoisted contract.

    `hoist_target_path` is `contracts.hoist_target_path`, computed once in §3.1 step 5b and
    persisted; §3.3 is explicit that Phase 2 and Phase 3 never recompute it, so it is carried
    here rather than re-derived from a `ContractKind` table this module must not know.
    """

    node_id: str
    ecosystem: Ecosystem
    published: Coordinate | None = None
    dest_override: str | None = None
    hoist_target_path: str | None = None


def normalize_dest(dest: str) -> str:
    """A monorepo-relative POSIX dir with no leading/trailing/duplicate separators.

    Rejects `..` and absolute paths outright: `dest` is joined into the integration worktree by
    Phase 3 step 1, so a traversal segment reaching it is a write outside the monorepo.
    """
    if dest.startswith("/"):
        raise ValueError(f"dest must be monorepo-relative, got {dest!r}")
    parts = [segment for segment in dest.split("/") if segment not in ("", ".")]
    if not parts:
        raise ValueError("dest must be non-empty")
    if ".." in parts:
        raise ValueError(f"dest must not escape the monorepo root, got {dest!r}")
    return "/".join(parts)


def is_reserved_dest(dest: str) -> bool:
    """True when any segment is `_scc` — the namespace §3.1 6e's union targets own."""
    return RESERVED_SCC_SEGMENT in dest.split("/")


def select_primary_coordinate(candidates: Sequence[PublishedCoordinate]) -> Coordinate | None:
    """§3.3's "primary published coordinate": the one with the most inbound edges.

    Ties break on `Coordinate.key`, never on input order — a `dest` that depends on the order
    rows came back from SQLite is a `dest` that moves between runs (§11.6).
    """
    if not candidates:
        return None
    best = min(candidates, key=lambda c: (-c.inbound_edges, c.coordinate.key))
    return best.coordinate


def layout(node: LayoutNode, registry: EcosystemRegistry) -> str:
    """`layout(node)` → its monorepo-relative destination directory (§3.3).

    `dest_override` from `config/repos.yaml` wins, then a contract's persisted
    `hoist_target_path`, then `monorepo_dir / path_tail(primary coordinate)`. A node with no
    primary published coordinate is routed through the *same* registry lookup with a coordinate
    synthesized from its `node_id`, so the unknown-ecosystem path of §3.1 step 2 is a value here,
    not a branch.
    """
    explicit = node.dest_override or node.hoist_target_path
    if explicit is not None:
        dest = normalize_dest(explicit)
    else:
        adapter = registry.for_ecosystem(node.ecosystem)
        coordinate = node.published or Coordinate(ecosystem=node.ecosystem, name=node.node_id)
        dest = normalize_dest(f"{adapter.monorepo_dir}/{adapter.path_tail(coordinate)}")
    if is_reserved_dest(dest):
        raise ReservedDestError(node.node_id, dest)
    return dest


def scc_package(ecosystem: Ecosystem, scc_id: str, registry: EcosystemRegistry) -> str:
    """Where an `ATOMIC_WAVE` SCC's single coarsened target lives, keyed by the **pair**.

    Delegates to `graph.cycles.scc_dest` so the one definition of the reserved path serves both
    the sequencer that reserves it and the emitter that writes into it.
    """
    return scc_dest(
        ecosystem, scc_id, monorepo_dir=registry.for_ecosystem(ecosystem).monorepo_dir
    )


def scc_label(ecosystem: Ecosystem, scc_id: str, registry: EcosystemRegistry) -> str:
    """`//<scc_dest>:<scc_id>`, with the `scc:` prefix folded — `:` is the label separator and
    cannot appear inside a target name, so `//java/_scc/scc:ab…` is not a legal label at all."""
    return f"//{scc_package(ecosystem, scc_id, registry)}:{scc_target_name(scc_id)}"


def stub_dest(coord_key: str) -> str:
    """`third_party/stubs/<coord>` for an abandoned dependency's generated stub (§3.5)."""
    slug = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in coord_key)
    return f"{STUB_ROOT}/{slug}"


def skeleton_paths() -> tuple[str, ...]:
    """The fixed, pipeline-owned skeleton — root files first, then directories, both sorted."""
    return tuple(sorted(MODULE_FILES)) + tuple(sorted(SKELETON_DIRS))
