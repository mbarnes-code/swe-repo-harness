"""Fixture-only `ManifestAdapter` for a Ruby `Gemfile`/`*.gemspec` (SPEC §12.34 Clause A, §1
touchpoint 1).

Not a real language integration. §1 requires exactly three abstract methods on a
`ManifestAdapter` (`matches`, `parse`, `coordinate`) plus the `name`/`ecosystem` `ClassVar`s
(`src/fleet/manifests/base.py`) — this fixture supplies the minimum that proves the touchpoint
without claiming to extract real Ruby dependency edges (`parse()` returns `[]`; research-23's
report, Q2, calls this a legitimate simplification: dependency-edge extraction is not what §12.34
Clause A needs to prove).

**The judgment call (research-23's "Judgment call the SPEC doesn't resolve"), Agent
Recommendation, not a directive:** the fixture's `ecosystem` `ClassVar` must be the TEST's decoy
`Ecosystem.RUBY` member (SPEC §12.34: "one `Ecosystem` member injected via the test's
enum-extension fixture"), which does not exist at THIS module's import time — the decoy is built
by the test, after this module is already imported. `make_ruby_manifest_adapter` below is the
factory-pattern resolution: it returns a fresh subclass with `ecosystem` bound to whatever member
the caller passes in, so the test builds its decoy `Ecosystem` first and calls this factory
second. The alternative research-23 also names (a late-binding mutable class slot, monkeypatched
onto the class after the fact) was not chosen: the factory keeps this file free of test-only
monkeypatch mechanics of its own, and matches research-23's own live-verified proof script, which
built its inline fixture class the same way — fully formed, inside the already-patched scope.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from fleet.manifests.base import ManifestAdapter
from fleet.models.enums import Ecosystem
from fleet.models.repo import Coordinate, RawDependency

_GEMSPEC_SUFFIX = ".gemspec"


class _RubyManifestAdapterBase(ManifestAdapter):
    """Everything about this fixture adapter that does NOT depend on the decoy `Ecosystem`
    member; `make_ruby_manifest_adapter` below subclasses this to bind `ecosystem`."""

    name: ClassVar[str] = "ruby_gemfile"
    version: ClassVar[int] = 1
    priority: ClassVar[int] = 100

    def matches(self, path: Path) -> bool:
        return path.name == "Gemfile" or path.suffix == _GEMSPEC_SUFFIX

    def parse(self, path: Path) -> list[RawDependency]:
        """Static parse only (§7.3) — always `[]`. This fixture proves the touchpoint count, not
        dependency-edge extraction; research-23 Q2 calls returning `[]` here a legitimate
        simplification for exactly this reason."""
        _ = path
        return []

    def coordinate(self, raw: RawDependency) -> Coordinate:
        return Coordinate(ecosystem=self.ecosystem, group="", name=raw.raw_id)


def make_ruby_manifest_adapter(ruby_member: Ecosystem) -> type[ManifestAdapter]:
    """Bind `ecosystem` to the caller's decoy `Ecosystem.RUBY` member and return a fresh class.

    The caller (the test) registers the returned class with `fleet.manifests.base.register`
    itself — this factory only builds the class, per the SPEC's "injected via the test's
    enum-extension fixture" phrasing (the injection point is the test, not this fixture module).
    """
    return type(
        "RubyManifestAdapter",
        (_RubyManifestAdapterBase,),
        {"ecosystem": ruby_member, "__module__": __name__},
    )
