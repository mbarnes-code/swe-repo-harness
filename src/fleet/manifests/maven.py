"""Maven `pom.xml` adapter (ADR-0005).

Static XML parse of ONE pom. No effective-POM computation: `mvn help:effective-pom` is a
resolver invocation that downloads parent POMs, and this package is offline by construction.
Property interpolation is therefore limited to what the file itself states (§ECOSYSTEM_LIMITS
in the module docstring below).

`<parent>` is emitted as a dependency with `scope="parent"`: a parent POM is an ordering
constraint exactly as strong as a compile dependency, and Phase 1 would under-order without it.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import ClassVar, Final

from fleet.manifests.base import (
    ManifestAdapter,
    ManifestParseError,
    read_text,
    register,
)
from fleet.models.enums import Ecosystem
from fleet.models.repo import Coordinate, RawDependency

_PROPERTY: Final = re.compile(r"\$\{([^}]+)\}")
_MAX_INTERPOLATION_DEPTH: Final = 10
# Rejected outright rather than parsed: a cloned third-party pom is untrusted input, and
# `expat` will happily expand an internal entity bomb. No real pom declares a DOCTYPE.
_DOCTYPE: Final = re.compile(r"<!(DOCTYPE|ENTITY)\b", re.IGNORECASE)


@register
class MavenAdapter(ManifestAdapter):
    """`pom.xml` — `<parent>`, `<dependencies>` and `<dependencyManagement>`."""

    name: ClassVar[str] = "maven"
    ecosystem: ClassVar[Ecosystem] = Ecosystem.MAVEN
    version: ClassVar[int] = 1
    priority: ClassVar[int] = 100

    def matches(self, path: Path) -> bool:
        return path.name == "pom.xml"

    def parse(self, path: Path) -> list[RawDependency]:
        root = _root(path)
        props = _properties(root)
        out: list[RawDependency] = []

        parent = _child(root, "parent")
        if parent is not None:
            out.append(_dependency(parent, props, path=path, default_scope="parent"))

        for container, default_scope in (
            (root, "compile"),
            (_child(root, "dependencyManagement"), "managed"),
        ):
            if container is None:
                continue
            deps = _child(container, "dependencies")
            if deps is None:
                continue
            for node in deps:
                if _tag(node) != "dependency":
                    continue
                out.append(_dependency(node, props, path=path, default_scope=default_scope))
        return out

    def coordinate(self, raw: RawDependency) -> Coordinate:
        group, name = _split_ga(raw.raw_id)
        return Coordinate(
            ecosystem=Ecosystem.MAVEN,
            group=group,
            name=name,
            version_spec=raw.version_spec,
        )

    def publishes(self, path: Path) -> Coordinate | None:
        root = _root(path)
        props = _properties(root)
        artifact = _text(root, "artifactId", props)
        if artifact is None:
            return None
        parent = _child(root, "parent")
        group = _text(root, "groupId", props)
        version = _text(root, "version", props)
        if parent is not None:  # both are inherited from the parent when omitted
            group = group or _text(parent, "groupId", props)
            version = version or _text(parent, "version", props)
        return Coordinate(
            ecosystem=Ecosystem.MAVEN,
            group=group or "",
            name=artifact,
            version_spec=version,
        )


def _root(path: Path) -> ET.Element:
    text = read_text(path)
    if _DOCTYPE.search(text):
        raise ManifestParseError(f"{path}: refusing to parse a pom declaring DOCTYPE/ENTITY")
    try:
        return ET.fromstring(text)  # noqa: S314 — DOCTYPE/ENTITY rejected immediately above
    except ET.ParseError as exc:
        raise ManifestParseError(f"{path}: malformed XML: {exc}") from exc


def _tag(node: ET.Element) -> str:
    """Local name, ignoring the `http://maven.apache.org/POM/4.0.0` default namespace."""
    return node.tag.rpartition("}")[2]


def _child(node: ET.Element, name: str) -> ET.Element | None:
    return next((c for c in node if _tag(c) == name), None)


def _properties(root: ET.Element) -> dict[str, str]:
    node = _child(root, "properties")
    props = {_tag(c): (c.text or "").strip() for c in node} if node is not None else {}
    # The two built-ins that appear in real poms often enough to matter for identity. Both are
    # inherited from <parent> when the module omits them, which is the common multi-module
    # layout — resolving only the literal element would leave `${project.version}` verbatim in
    # every child module of every reactor build in the fleet.
    parent = _child(root, "parent")
    for builtin, source in (("project.groupId", "groupId"), ("project.version", "version")):
        for owner in (root, parent):
            if owner is None:
                continue
            literal = _child(owner, source)
            if literal is not None and literal.text and literal.text.strip():
                props.setdefault(builtin, literal.text.strip())
                break
    return props


def _interpolate(value: str, props: dict[str, str]) -> str:
    """Resolve `${...}` against this pom's own `<properties>`, bounded. Unresolved references
    are left verbatim — `version_spec` is documented as "raw, as written", and inventing a
    value would be worse than reporting the placeholder."""
    for _ in range(_MAX_INTERPOLATION_DEPTH):
        if "${" not in value:
            break
        replaced = _PROPERTY.sub(lambda m: props.get(m.group(1), m.group(0)), value)
        if replaced == value:
            break
        value = replaced
    return value


def _text(node: ET.Element, name: str, props: dict[str, str]) -> str | None:
    child = _child(node, name)
    if child is None or not child.text or not child.text.strip():
        return None
    return _interpolate(child.text.strip(), props)


def _dependency(
    node: ET.Element, props: dict[str, str], *, path: Path, default_scope: str
) -> RawDependency:
    group = _text(node, "groupId", props)
    artifact = _text(node, "artifactId", props)
    if not group or not artifact:
        raise ManifestParseError(
            f"{path}: <{_tag(node)}> is missing groupId or artifactId "
            f"(groupId={group!r}, artifactId={artifact!r})"
        )
    version = _text(node, "version", props)
    raw_id = f"{group}:{artifact}:{version}" if version else f"{group}:{artifact}"
    return RawDependency(
        raw_id=raw_id,
        version_spec=version,
        scope=_text(node, "scope", props) or default_scope,
        optional=(_text(node, "optional", props) or "").lower() == "true",
    )


def _split_ga(raw_id: str) -> tuple[str, str]:
    group, _, rest = raw_id.partition(":")
    artifact = rest.partition(":")[0]
    return group, artifact
