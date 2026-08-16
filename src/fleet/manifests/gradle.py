"""Gradle `build.gradle[.kts]` + `gradle/libs.versions.toml` adapter (ADR-0005).

A `build.gradle` is a *program*, not a manifest, and the only sound way to know what it declares
is to run it — which this package may not do (offline, deterministic, no shelling out to
Gradle). So the script is read with a line regex that recognizes the declaration forms that
cover the overwhelming majority of real builds, and the version catalog (`libs.versions.toml`),
which IS a manifest, is parsed exactly.

Everything the regex cannot see is listed in `UNPARSED_FORMS` below rather than pretended away:
`ManifestRef.low_confidence` plus the ADR-0008 class-2 LLM extraction slot is the designed
answer to a build script that outsmarts a regex — silently returning fewer edges is not.
"""

from __future__ import annotations

import re
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

UNPARSED_FORMS: Final[tuple[str, ...]] = (
    "dependencies built by a loop, a function, or an `ext`/`var` interpolation",
    "`libs.foo.bar` version-catalog accessors (the accessor is resolved by Gradle, not by us)",
    "`project(':other')` module dependencies (internal to the repo, not a fleet edge)",
    "dependencies added by a convention plugin or an applied script",
)

_CONFIGURATIONS: Final[tuple[str, ...]] = (
    "annotationProcessor",
    "api",
    "classpath",
    "compile",
    "compileOnly",
    "compileOnlyApi",
    "developmentOnly",
    "implementation",
    "kapt",
    "ksp",
    "runtimeOnly",
    "testAnnotationProcessor",
    "testCompile",
    "testCompileOnly",
    "testImplementation",
    "testRuntimeOnly",
)
_CONFIG_ALT: Final = "|".join(_CONFIGURATIONS)

# `implementation "g:a:v"` / `implementation('g:a:v')` — Groovy and Kotlin DSL, both quote styles.
_GAV_LINE: Final = re.compile(
    rf"""^\s*(?P<config>{_CONFIG_ALT})\s*\(?\s*
        (?P<quote>['"])(?P<gav>[^'"]+)(?P=quote)""",
    re.VERBOSE,
)
# `implementation group: 'g', name: 'a', version: 'v'` — the Groovy map form.
_MAP_LINE: Final = re.compile(
    rf"""^\s*(?P<config>{_CONFIG_ALT})\s*\(?\s*
        group\s*:\s*(?P<qg>['"])(?P<group>[^'"]+)(?P=qg)\s*,\s*
        name\s*:\s*(?P<qn>['"])(?P<name>[^'"]+)(?P=qn)
        (?:\s*,\s*version\s*:\s*(?P<qv>['"])(?P<version>[^'"]+)(?P=qv))?""",
    re.VERBOSE,
)
_GAV_SHAPE: Final = re.compile(r"^[^:\s]+:[^:\s]+(:[^:\s]*)?$")


@register
class GradleAdapter(ManifestAdapter):
    """`build.gradle`, `build.gradle.kts` and `gradle/libs.versions.toml`."""

    name: ClassVar[str] = "gradle"
    ecosystem: ClassVar[Ecosystem] = Ecosystem.GRADLE
    version: ClassVar[int] = 1
    priority: ClassVar[int] = 100

    def matches(self, path: Path) -> bool:
        return path.name in {"build.gradle", "build.gradle.kts", "libs.versions.toml"}

    def parse(self, path: Path) -> list[RawDependency]:
        if path.name == "libs.versions.toml":
            return _parse_catalog(path)
        return _parse_script(path)

    def coordinate(self, raw: RawDependency) -> Coordinate:
        group, _, rest = raw.raw_id.partition(":")
        return Coordinate(
            ecosystem=Ecosystem.GRADLE,
            group=group,
            name=rest.partition(":")[0],
            version_spec=raw.version_spec,
        )


def _parse_script(path: Path) -> list[RawDependency]:
    out: list[RawDependency] = []
    for lineno, line in enumerate(read_text(path).splitlines(), start=1):
        if line.lstrip().startswith("//"):
            continue
        map_match = _MAP_LINE.match(line)
        if map_match is not None:
            version = map_match.group("version")
            group = map_match.group("group")
            name = map_match.group("name")
            raw_id = f"{group}:{name}:{version}" if version else f"{group}:{name}"
            out.append(
                RawDependency(
                    raw_id=raw_id,
                    version_spec=version,
                    scope=map_match.group("config"),
                    source_line=lineno,
                )
            )
            continue
        gav_match = _GAV_LINE.match(line)
        if gav_match is None:
            continue
        gav = gav_match.group("gav").strip()
        if not _GAV_SHAPE.match(gav):
            continue  # a plugin id, a file path, or a catalog accessor — not a coordinate
        parts = gav.split(":")
        out.append(
            RawDependency(
                raw_id=gav,
                version_spec=parts[2] if len(parts) > 2 and parts[2] else None,
                scope=gav_match.group("config"),
                source_line=lineno,
            )
        )
    return out


def _parse_catalog(path: Path) -> list[RawDependency]:
    text = read_text(path)
    try:
        parsed: object = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestParseError(f"{path}: invalid version catalog TOML: {exc}") from exc
    doc = as_table(parsed, path=path, what="version catalog root")
    versions = {
        alias: opt_str(value) or ""
        for alias, value in as_table(doc.get("versions", {}), path=path, what="[versions]").items()
    }
    libraries = as_table(doc.get("libraries", {}), path=path, what="[libraries]")

    out: list[RawDependency] = []
    for alias in sorted(libraries):
        entry = libraries[alias]
        if isinstance(entry, str):
            parts = entry.split(":")
            if len(parts) < 2:
                raise ManifestParseError(
                    f"{path}: [libraries] {alias!r} is not a 'group:artifact[:version]' string: "
                    f"{entry!r}"
                )
            out.append(
                RawDependency(
                    raw_id=entry,
                    version_spec=parts[2] if len(parts) > 2 and parts[2] else None,
                    scope="catalog",
                )
            )
            continue
        table = as_table(entry, path=path, what=f"[libraries.{alias}]")
        module = opt_str(table.get("module"))
        if module is None:
            group = opt_str(table.get("group"))
            name = opt_str(table.get("name"))
            if group is None or name is None:
                raise ManifestParseError(
                    f"{path}: [libraries.{alias}] needs `module` or both `group` and `name`"
                )
            module = f"{group}:{name}"
        version = _catalog_version(table.get("version"), versions)
        out.append(
            RawDependency(
                raw_id=f"{module}:{version}" if version else module,
                version_spec=version,
                scope="catalog",
            )
        )
    return out


def _catalog_version(node: object, versions: dict[str, str]) -> str | None:
    """`version = "1.2"`, `version.ref = "alias"`, or `version = { require = "[1.0,2.0)" }`."""
    direct = opt_str(node)
    if direct is not None:
        return direct
    if not isinstance(node, dict):
        return None
    ref = opt_str(node.get("ref"))
    if ref is not None:
        return versions.get(ref) or None
    for key in ("require", "strictly", "prefer"):
        found = opt_str(node.get(key))
        if found is not None:
            return found
    return None
