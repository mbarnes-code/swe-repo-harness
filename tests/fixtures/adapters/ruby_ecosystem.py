"""Fixture-only `EcosystemAdapter` for Ruby (SPEC §12.34 Clause A, §1 touchpoint 2).

Not a real language integration. Emits a `ruby_library` `BuildTarget` from `generate_targets()`
and a `rules_ruby` `WorkspaceDep` from `workspace_deps()` — the two emission clauses §12.34's
Clause A text names — using the minimal 5-abstract-method surface research-23's report (Q2)
identifies on `EcosystemAdapter` (`src/fleet/ecosystems/base.py`).

`contract_bindings` binds exactly one shipped `ContractKind` (`OPENAPI`) and deliberately omits
`PROTO` — reserved for a future Clause B extension of this same fixture (a separate, later task;
research-23 found Clause B's driver-side mechanism does not exist in `src/fleet/` yet, so nothing
in `src/fleet/` would ever read this map's missing `PROTO` entry today).

**The factory-pattern judgment call** is the same one `ruby_manifest.py` documents: `ecosystems`
must be a `frozenset` containing the test's decoy `Ecosystem.RUBY` member, which does not exist at
this module's import time, so `make_ruby_ecosystem_adapter` binds it at call time instead of at
class-body time. See `ruby_manifest.py`'s docstring for the full Agent Recommendation and why the
alternative (a late-binding mutable slot) was not chosen.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from fleet.ecosystems.base import EcosystemAdapter, path_segment, target_name
from fleet.models.build import BuildTarget, BuildUnit, WorkspaceDep
from fleet.models.enums import ContractKind, Ecosystem
from fleet.models.repo import Coordinate

#: The one external gem this fixture always declares a `WorkspaceDep` for — unconditional on the
#: unit's own declared dependencies, for the same reason `ruby_manifest.py`'s `parse()` always
#: returns `[]`: this fixture proves the touchpoint's SHAPE (a `rules_ruby` `WorkspaceDep` exists
#: at all), not real gem dependency-edge extraction.
_FIXTURE_GEM = "railties"


class _RubyEcosystemAdapterBase(EcosystemAdapter):
    """Everything about this fixture adapter that does NOT depend on the decoy `Ecosystem`
    member; `make_ruby_ecosystem_adapter` below subclasses this to bind `ecosystems`."""

    name: ClassVar[str] = "ruby"
    monorepo_dir: ClassVar[str] = "ruby"
    ruleset: ClassVar[str | None] = "rules_ruby"
    extension: ClassVar[str | None] = "gem.install"
    extension_bzl: ClassVar[Mapping[str, str]] = {"gem": "@rules_ruby//gem:extensions.bzl"}
    """Required: `bazel/generators.render_module_bazel` raises for a `use_extension` proxy
    (`gem`, from `workspace_deps()`'s `extension="gem.install"`) that neither the caller nor this
    map can place — "a plausible-looking label that dies inside Bazel's loader" is exactly the
    failure that check exists to convert into a loud, attributable error instead (Rule 11). The
    label itself is never read by anything in this test (`FakeBazel` stands in for real Bazel), so
    it is a plausible placeholder, not a verified `rules_ruby` fact."""
    library_rule: ClassVar[str] = "ruby_library"
    library_bzl: ClassVar[str | None] = "@rules_ruby//ruby:defs.bzl"
    contract_bindings: ClassVar[Mapping[ContractKind, str]] = {ContractKind.OPENAPI: "genrule"}

    def path_tail(self, coordinate: Coordinate) -> str:
        return path_segment(coordinate.name)

    def import_specifier(self, coordinate: Coordinate, dest: str) -> str:
        _ = dest
        return coordinate.name

    def workspace_deps(self, unit: BuildUnit) -> list[WorkspaceDep]:
        _ = unit
        ruby_member = next(iter(self.ecosystems))
        return [
            WorkspaceDep(
                ruleset="rules_ruby",
                extension="gem.install",
                coordinate=Coordinate(ecosystem=ruby_member, group="", name=_FIXTURE_GEM),
                repo_name="gems",
                attrs={"name": "gems"},
            )
        ]

    def generate_targets(self, unit: BuildUnit) -> list[BuildTarget]:
        return [
            BuildTarget(
                package=unit.dest,
                name=target_name(unit),
                rule="ruby_library",
                load_from="@rules_ruby//ruby:defs.bzl",
                srcs=self.sources(unit),
                deps=self.dep_labels(unit),
            )
        ]

    def test_targets(self, unit: BuildUnit) -> list[BuildTarget]:
        return []


def make_ruby_ecosystem_adapter(ruby_member: Ecosystem) -> type[EcosystemAdapter]:
    """Bind `ecosystems` to `frozenset({ruby_member})` and return a fresh class.

    The caller (the test) registers the returned class with `fleet.ecosystems.base.register`
    itself, mirroring `ruby_manifest.py`'s `make_ruby_manifest_adapter`.
    """
    return type(
        "RubyEcosystemAdapter",
        (_RubyEcosystemAdapterBase,),
        {"ecosystems": frozenset({ruby_member}), "__module__": __name__},
    )
