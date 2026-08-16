"""npm/pnpm/yarn `package.json` adapter (ADR-0005).

Static JSON parse only. The lockfiles (`package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`) are
deliberately NOT read: they carry the *resolved* tree, which is a fact about the day someone
last ran `npm install`, whereas the graph wants the *declared* edge. `matches` therefore claims
`package.json` and nothing else.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar, Final

from fleet.manifests.base import (
    ManifestAdapter,
    ManifestParseError,
    as_table,
    opt_str,
    read_text,
    register,
)
from fleet.models.enums import Ecosystem
from fleet.models.repo import Coordinate, RawDependency

# Section -> scope, in the fixed order they are emitted. Ordering is part of the contract:
# the same bytes must produce the same list, in the same order, on every host.
_SECTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("dependencies", "prod"),
    ("devDependencies", "dev"),
    ("peerDependencies", "peer"),
    ("optionalDependencies", "optional"),
)


@register
class NpmAdapter(ManifestAdapter):
    """`package.json` — declared dependencies only, never the lockfile's resolved tree."""

    name: ClassVar[str] = "npm"
    ecosystem: ClassVar[Ecosystem] = Ecosystem.NPM
    version: ClassVar[int] = 1
    priority: ClassVar[int] = 100

    def matches(self, path: Path) -> bool:
        return path.name == "package.json"

    def parse(self, path: Path) -> list[RawDependency]:
        doc = self._document(path)
        out: list[RawDependency] = []
        for section, scope in _SECTIONS:
            raw_section = doc.get(section)
            if raw_section is None:
                continue
            table = as_table(raw_section, path=path, what=section)
            for pkg in sorted(table):
                if not pkg:
                    raise ManifestParseError(f"{path}: empty package name in {section!r}")
                out.append(
                    RawDependency(
                        raw_id=pkg,
                        version_spec=opt_str(table[pkg]),
                        scope=scope,
                        optional=section == "optionalDependencies",
                    )
                )
        return out

    def coordinate(self, raw: RawDependency) -> Coordinate:
        group, name = _split_scope(raw.raw_id)
        return Coordinate(
            ecosystem=Ecosystem.NPM,
            group=group,
            name=name,
            version_spec=raw.version_spec,
        )

    def publishes(self, path: Path) -> Coordinate | None:
        doc = self._document(path)
        declared = opt_str(doc.get("name"))
        if declared is None:
            return None  # a private workspace root may legitimately omit `name`
        group, name = _split_scope(declared)
        return Coordinate(
            ecosystem=Ecosystem.NPM,
            group=group,
            name=name,
            version_spec=opt_str(doc.get("version")),
        )

    def _document(self, path: Path) -> dict[str, object]:
        text = read_text(path)
        try:
            parsed: object = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ManifestParseError(
                f"{path}: invalid JSON at line {exc.lineno}: {exc.msg}"
            ) from exc
        return as_table(parsed, path=path, what="package.json root")


def _split_scope(spec: str) -> tuple[str, str]:
    """`@acme/ui` -> (`@acme`, `ui`); `react` -> (``, `react`).

    The `@` is kept in the group because `Coordinate.group_path` strips it when building the
    monorepo layout, and stripping it here too would make `@acme` and `acme` collide.
    """
    if spec.startswith("@") and "/" in spec:
        scope, _, bare = spec.partition("/")
        return scope, bare
    return "", spec
