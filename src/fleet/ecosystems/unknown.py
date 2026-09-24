"""`UNKNOWN` → `misc/`: one `filegroup`, no external deps — the §3.1 step 2 fallback path.

The exact counterpart of `manifests/unknown.py`. That adapter is what stops a repo with no
recognizable manifest from vanishing from the inventory; this one is what stops the same repo
from vanishing from the monorepo. Together they are why `layout()` is total *without a null
check anywhere above it* (§3.3): the unknown repo is a value in the registry, not a branch in
the caller.

**This is the degraded path and it says so.** `degraded = True` and the `fleet_adapter=unknown`
tag are both mandatory: a `filegroup` emitted because no language was recognized must never be
mistakable — in a `BuildPlan`, in a finding, or in a reviewer's reading of a generated
`BUILD.bazel` — for one an adapter chose. Everything this adapter emits is honest about being
the floor: sources are *grouped*, not compiled, and nothing here claims a rule this harness
could not honour.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar, Final

from fleet.ecosystems.base import EcosystemAdapter, path_segment, register, target_name
from fleet.models.build import BuildTarget, BuildUnit, WorkspaceDep
from fleet.models.enums import ContractKind, Ecosystem
from fleet.models.repo import Coordinate

DEGRADED_TAG: Final = "fleet_adapter=unknown"
"""The tag stamped on every target this adapter emits. `bazel query 'attr(tags,
"fleet_adapter=unknown", //...)'` is then the whole list of packages that got the floor —
which is the number a Phase 4 reviewer needs and cannot otherwise obtain."""


@register
class UnknownAdapter(EcosystemAdapter):
    """The catch-all. A repo is never silently dropped (§3.1 step 2), and never silently
    pretended to be built either."""

    name: ClassVar[str] = "unknown"
    ecosystems: ClassVar[frozenset[Ecosystem]] = frozenset({Ecosystem.UNKNOWN})
    monorepo_dir: ClassVar[str] = "misc"
    ruleset: ClassVar[str | None] = None
    extension: ClassVar[str | None] = None
    repo_name: ClassVar[str | None] = None
    library_rule: ClassVar[str] = "filegroup"
    library_bzl: ClassVar[str | None] = None  # native rule; a load() here would not resolve
    degraded: ClassVar[bool] = True
    contract_bindings: ClassVar[Mapping[ContractKind, str]] = {}
    """Empty on purpose: an unknown-language consumer of an IDL has no binding rule, and §13
    row 31 makes that a `ContractBindingUnavailable` finding rather than an invented rule."""

    def path_tail(self, coordinate: Coordinate) -> str:
        """`<repo_id>` — `layout()` synthesizes `Coordinate(name=node_id)` for a node with no
        primary published coordinate, so this is the repo id by construction."""
        return path_segment(coordinate.name)

    def import_specifier(self, coordinate: Coordinate, dest: str) -> str:
        """The unit's monorepo path — because there is no language here and therefore no import.

        The honest answer to "what does a cross-repo import of an unrecognized repo become" is
        that this adapter cannot know: it emits a `filegroup`, nothing compiles, and no import
        system is involved. A path is what the files are actually reachable by, and it is
        deliberately not a Bazel label: a label returned from the degraded path would be the
        exact defect this method exists to prevent, arriving under the one adapter whose output
        nobody scrutinises.
        """
        _ = coordinate
        return dest.strip("/")

    def workspace_deps(self, unit: BuildUnit) -> list[WorkspaceDep]:
        """`[]` — declares no external deps.

        An UNKNOWN coordinate has no registry to fetch it from, so any `WorkspaceDep` emitted
        here would name a `bazel_dep` that does not exist and fail MODULE.bazel evaluation for
        the *whole* monorepo, not just for this repo. The sources are still merged and still
        reachable as a `filegroup`; what is lost is compilation, and that loss is disclosed.
        """
        return []

    def generate_targets(self, unit: BuildUnit) -> list[BuildTarget]:
        """Exactly one `filegroup` over everything, tagged as the degraded emission.

        Test sources are included in the group rather than split into a `test_targets` rule:
        there is no test runner to name, and a `filegroup` a reviewer can see is worth more than
        a second empty group.
        """
        srcs = sorted({*self.sources(unit), *self.test_sources(unit), *self.non_source_files(unit)})
        return [
            BuildTarget(
                package=unit.dest,
                name=target_name(unit),
                rule="filegroup",
                srcs=srcs,
                attrs={"tags": [DEGRADED_TAG]},
                visibility=["//visibility:public"],
            )
        ]

    def test_targets(self, unit: BuildUnit) -> list[BuildTarget]:
        """`[]` — no rule here can run a test, and emitting a target that always passes would
        turn §3.3's `bazel test` success criterion into a tautology for every unknown repo."""
        return []
