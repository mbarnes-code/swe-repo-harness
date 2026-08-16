"""Last-resort adapter (priority 10_000): the no-manifest path (§3.1 step 2 fallback).

Claims any file no other adapter wanted so that a repo with no recognizable manifest still
produces `Ecosystem.UNKNOWN` inventory rows and appears in the fleet, rather than silently
vanishing from the graph. `matches` is unconditionally True; priority 10 000 is what makes that
safe — this adapter can only ever be offered a path every other adapter has already declined.

`parse` returning `[]` is the ONE place in this package where an empty list is not a defect: it
is the literal claim "this file declares no dependencies we can read", and the caller pairs it
with `low_confidence = True` so the ADR-0008 class-2 LLM extraction slot gets a look. Every
other adapter raises `ManifestParseError` instead of shrugging.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from fleet.manifests.base import ManifestAdapter, register
from fleet.models.enums import Ecosystem
from fleet.models.repo import Coordinate, RawDependency


@register
class UnknownAdapter(ManifestAdapter):
    """The catch-all. A repo is never silently dropped (§3.1 step 2)."""

    name: ClassVar[str] = "unknown"
    ecosystem: ClassVar[Ecosystem] = Ecosystem.UNKNOWN
    version: ClassVar[int] = 1
    priority: ClassVar[int] = 10_000

    def matches(self, path: Path) -> bool:
        return True

    def parse(self, path: Path) -> list[RawDependency]:
        return []

    def coordinate(self, raw: RawDependency) -> Coordinate:
        """Whatever text the extraction slot produced, addressed in the UNKNOWN namespace."""
        return Coordinate(
            ecosystem=Ecosystem.UNKNOWN,
            group="",
            name=raw.raw_id,
            version_spec=raw.version_spec,
        )
