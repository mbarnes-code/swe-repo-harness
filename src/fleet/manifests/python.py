"""Python `pyproject.toml` / `setup.cfg` / `requirements*.txt` adapter (ADR-0005).

PEP 508 requirement strings are parsed with a local regex rather than `packaging`: `packaging`
is not a declared dependency of this project (see `pyproject.toml`), and the only thing the
graph needs from a requirement is its *name* — the version specifier is carried verbatim as
`version_spec` and never compared, because `Coordinate.key` deliberately excludes version
(ADR-0017). Adding a dependency to read one field would be the wrong trade.
"""

from __future__ import annotations

import configparser
import re
import tomllib
from pathlib import Path
from typing import ClassVar, Final

from fleet.manifests.base import (
    ManifestAdapter,
    ManifestParseError,
    as_table,
    read_text,
    register,
)
from fleet.models.enums import Ecosystem
from fleet.models.repo import Coordinate, RawDependency

# PEP 508: name, optional extras, then everything else (specifier and/or `; marker`).
_REQUIREMENT: Final = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?P<extras>\[[^\]]*\])?\s*(?P<rest>.*)$"
)
# PEP 503 canonical form: runs of `-`, `_` and `.` collapse to a single `-`, then case-fold.
_SEPARATORS: Final = re.compile(r"[-_.]+")
# What may legally follow a PEP 508 name: a specifier, a marker, a direct URL, or a legacy
# parenthesised specifier. Anything else means the "name" we matched was the head of something
# that is not a requirement at all (a bare `git+https://…` URL, say) and must fail loud.
_LEGAL_AFTER_NAME: Final = "<>=!~;@(,"


@register
class PythonAdapter(ManifestAdapter):
    """PEP 621 `[project]`, PEP 735 `[dependency-groups]`, `setup.cfg`, `requirements*.txt`."""

    name: ClassVar[str] = "python"
    ecosystem: ClassVar[Ecosystem] = Ecosystem.PYPI
    version: ClassVar[int] = 1
    priority: ClassVar[int] = 100

    def matches(self, path: Path) -> bool:
        stem = path.name
        return (
            stem in {"pyproject.toml", "setup.cfg"}
            or (stem.startswith("requirements") and stem.endswith(".txt"))
        )

    def parse(self, path: Path) -> list[RawDependency]:
        if path.name == "pyproject.toml":
            return _parse_pyproject(path)
        if path.name == "setup.cfg":
            return _parse_setup_cfg(path)
        return _parse_requirements(path)

    def coordinate(self, raw: RawDependency) -> Coordinate:
        return Coordinate(
            ecosystem=Ecosystem.PYPI,
            group="",  # PyPI is a flat namespace
            name=canonical_name(raw.raw_id),
            version_spec=raw.version_spec,
        )

    def publishes(self, path: Path) -> Coordinate | None:
        if path.name == "pyproject.toml":
            project = as_table(
                _document(path).get("project", {}), path=path, what="[project]"
            )
            name = project.get("name")
            version = project.get("version")
        elif path.name == "setup.cfg":
            parser = _read_cfg(path)
            name = parser.get("metadata", "name", fallback=None)
            version = parser.get("metadata", "version", fallback=None)
        else:
            return None  # a requirements file publishes nothing
        if not isinstance(name, str) or not name.strip():
            return None
        return Coordinate(
            ecosystem=Ecosystem.PYPI,
            group="",
            name=canonical_name(name.strip()),
            version_spec=version.strip() if isinstance(version, str) and version.strip() else None,
        )


def canonical_name(name: str) -> str:
    """PEP 503 normalization. `Flask_SQLAlchemy` and `flask-sqlalchemy` are one coordinate."""
    return _SEPARATORS.sub("-", name).lower()


def split_requirement(spec: str, *, path: Path, source_line: int | None = None) -> RawDependency:
    """Split one PEP 508 string into name + verbatim specifier. Fails loud on garbage."""
    text = spec.strip()
    match = _REQUIREMENT.match(text)
    where = f" (line {source_line})" if source_line else ""
    if match is None:
        raise ManifestParseError(f"{path}: not a PEP 508 requirement{where}: {spec!r}")
    rest = match.group("rest").strip()
    if rest and rest[0] not in _LEGAL_AFTER_NAME:
        raise ManifestParseError(f"{path}: not a PEP 508 requirement{where}: {spec!r}")
    version_spec = rest or None
    return RawDependency(
        raw_id=match.group("name"),
        version_spec=version_spec,
        source_line=source_line,
    )


def _document(path: Path) -> dict[str, object]:
    text = read_text(path)
    try:
        parsed: object = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestParseError(f"{path}: invalid TOML: {exc}") from exc
    return as_table(parsed, path=path, what="TOML root")


def _requirement_list(
    obj: object, *, path: Path, what: str, scope: str, optional: bool
) -> list[RawDependency]:
    """A PEP 621 / PEP 735 requirement array. Non-string entries (PEP 735 `include-group`
    tables) are skipped: they are indirections into the same file, not external edges."""
    if not isinstance(obj, list):
        raise ManifestParseError(
            f"{path}: expected an array for {what}, got {type(obj).__name__}"
        )
    out: list[RawDependency] = []
    for entry in obj:
        if not isinstance(entry, str):
            continue
        dep = split_requirement(entry, path=path)
        out.append(dep.model_copy(update={"scope": scope, "optional": optional}))
    return out


def _parse_pyproject(path: Path) -> list[RawDependency]:
    doc = _document(path)
    out: list[RawDependency] = []
    project = as_table(doc.get("project", {}), path=path, what="[project]")
    if "dependencies" in project:
        out += _requirement_list(
            project["dependencies"],
            path=path,
            what="project.dependencies",
            scope="main",
            optional=False,
        )
    extras = as_table(
        project.get("optional-dependencies", {}), path=path, what="[project.optional-dependencies]"
    )
    for extra in sorted(extras):
        out += _requirement_list(
            extras[extra],
            path=path,
            what=f"project.optional-dependencies.{extra}",
            scope=extra,
            optional=True,
        )
    groups = as_table(doc.get("dependency-groups", {}), path=path, what="[dependency-groups]")
    for group in sorted(groups):
        out += _requirement_list(
            groups[group],
            path=path,
            what=f"dependency-groups.{group}",
            scope=group,
            optional=True,
        )
    return out


def _read_cfg(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    try:
        parser.read_string(read_text(path), source=str(path))
    except configparser.Error as exc:
        raise ManifestParseError(f"{path}: invalid setup.cfg: {exc}") from exc
    return parser


def _parse_setup_cfg(path: Path) -> list[RawDependency]:
    parser = _read_cfg(path)
    out: list[RawDependency] = []
    install_requires = parser.get("options", "install_requires", fallback="")
    for line in install_requires.splitlines():
        if line.strip():
            out.append(
                split_requirement(line, path=path).model_copy(update={"scope": "main"})
            )
    if parser.has_section("options.extras_require"):
        for extra in sorted(parser.options("options.extras_require")):
            for line in parser.get("options.extras_require", extra).splitlines():
                if line.strip():
                    out.append(
                        split_requirement(line, path=path).model_copy(
                            update={"scope": extra, "optional": True}
                        )
                    )
    return out


def _parse_requirements(path: Path) -> list[RawDependency]:
    out: list[RawDependency] = []
    for lineno, raw_line in enumerate(read_text(path).splitlines(), start=1):
        line = raw_line.split(" #", 1)[0].strip()
        if not line or line.startswith(("#", "-")):
            continue  # blank, comment, or a pip option (`-r`, `-e`, `--index-url`)
        out.append(
            split_requirement(line, path=path, source_line=lineno).model_copy(
                update={"scope": "main"}
            )
        )
    return out
