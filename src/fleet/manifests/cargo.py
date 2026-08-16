"""Rust `Cargo.toml` adapter (ADR-0005).

Static TOML parse of the manifest only. `Cargo.lock` is not read (resolved tree, not declared
edge) and `cargo metadata` is never invoked (that is a resolver, and it downloads).

Both halves of a workspace are handled from the same file: `[workspace.dependencies]` in the
root manifest, and `dep = { workspace = true }` in a member — the member's inherited entry is
emitted with `version_spec = None`, because the member manifest genuinely does not state a
version and inventing one would put a fact in the graph that the bytes do not support.
"""

from __future__ import annotations

import tomllib
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

# Section -> scope, in fixed emission order.
_SECTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("dependencies", "normal"),
    ("dev-dependencies", "dev"),
    ("build-dependencies", "build"),
)


@register
class CargoAdapter(ManifestAdapter):
    """`Cargo.toml` — `[dependencies]`, `[target.*.dependencies]`, `[workspace.dependencies]`."""

    name: ClassVar[str] = "cargo"
    ecosystem: ClassVar[Ecosystem] = Ecosystem.CARGO
    version: ClassVar[int] = 1
    priority: ClassVar[int] = 100

    def matches(self, path: Path) -> bool:
        return path.name == "Cargo.toml"

    def parse(self, path: Path) -> list[RawDependency]:
        doc = _document(path)
        out: list[RawDependency] = []

        for section, scope in _SECTIONS:
            out += _section(doc.get(section), path=path, what=section, scope=scope)

        # `[target.'cfg(unix)'.dependencies]` — platform-gated, but still a real edge.
        targets = as_table(doc.get("target", {}), path=path, what="[target]")
        for triple in sorted(targets):
            table = as_table(targets[triple], path=path, what=f"[target.{triple}]")
            for section, scope in _SECTIONS:
                out += _section(
                    table.get(section),
                    path=path,
                    what=f"target.{triple}.{section}",
                    scope=f"target:{scope}",
                )

        workspace = as_table(doc.get("workspace", {}), path=path, what="[workspace]")
        out += _section(
            workspace.get("dependencies"),
            path=path,
            what="workspace.dependencies",
            scope="workspace",
        )
        return out

    def coordinate(self, raw: RawDependency) -> Coordinate:
        return Coordinate(
            ecosystem=Ecosystem.CARGO,
            group="",  # crates.io is a flat namespace
            name=raw.raw_id,
            version_spec=raw.version_spec,
        )

    def publishes(self, path: Path) -> Coordinate | None:
        package = as_table(_document(path).get("package", {}), path=path, what="[package]")
        name = opt_str(package.get("name"))
        if name is None:
            return None  # a virtual workspace root has no [package]
        return Coordinate(
            ecosystem=Ecosystem.CARGO,
            group="",
            name=name,
            version_spec=opt_str(package.get("version")),
        )


def _document(path: Path) -> dict[str, object]:
    try:
        parsed: object = tomllib.loads(read_text(path))
    except tomllib.TOMLDecodeError as exc:
        raise ManifestParseError(f"{path}: invalid TOML: {exc}") from exc
    return as_table(parsed, path=path, what="Cargo.toml root")


def _section(node: object, *, path: Path, what: str, scope: str) -> list[RawDependency]:
    if node is None:
        return []
    table = as_table(node, path=path, what=what)
    out: list[RawDependency] = []
    for alias in sorted(table):
        entry = table[alias]
        if isinstance(entry, str):
            out.append(RawDependency(raw_id=alias, version_spec=entry, scope=scope))
            continue
        detail = as_table(entry, path=path, what=f"{what}.{alias}")
        # `package = "real-crate"` renames on import: the coordinate is the REAL crate name,
        # not the local alias, or two repos depending on one crate would not join.
        crate = opt_str(detail.get("package")) or alias
        optional = detail.get("optional") is True
        out.append(
            RawDependency(
                raw_id=crate,
                version_spec=opt_str(detail.get("version")),
                scope=scope,
                optional=optional,
            )
        )
    return out
