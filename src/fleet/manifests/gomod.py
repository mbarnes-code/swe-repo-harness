"""Go `go.mod` adapter (ADR-0005).

`go.mod` has a small, line-oriented grammar, so this is a real parser rather than a set of
regexes: `module`, `require` (single and block form), `replace`, `exclude`, `retract`. `go list
-m all` is never invoked — it resolves the module graph over the network.

`// indirect` requirements are kept, marked `scope="indirect"`. They are edges: Go's MVS puts
the whole transitive closure in `go.mod`, and dropping them would silently under-order any repo
whose only path to a fleet sibling is indirect.
"""

from __future__ import annotations

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

_BLOCK_DIRECTIVES: Final[frozenset[str]] = frozenset(
    {"require", "replace", "exclude", "retract", "tool", "godebug"}
)


@register
class GomodAdapter(ManifestAdapter):
    """`go.mod` — the `module` line and every `require`, block or single."""

    name: ClassVar[str] = "gomod"
    ecosystem: ClassVar[Ecosystem] = Ecosystem.GO
    version: ClassVar[int] = 1
    priority: ClassVar[int] = 100

    def matches(self, path: Path) -> bool:
        return path.name == "go.mod"

    def parse(self, path: Path) -> list[RawDependency]:
        out: list[RawDependency] = []
        block: str | None = None
        for lineno, line, indirect in _lines(path):
            if block is not None:
                if line == ")":
                    block = None
                elif block == "require":
                    out.append(_requirement(line, path=path, lineno=lineno, indirect=indirect))
                continue
            directive, _, rest = line.partition(" ")
            if directive in _BLOCK_DIRECTIVES and rest.strip() == "(":
                block = directive
            elif directive == "require":
                out.append(_requirement(rest, path=path, lineno=lineno, indirect=indirect))
        if block is not None:
            raise ManifestParseError(f"{path}: unterminated `{block} (` block")
        return out

    def coordinate(self, raw: RawDependency) -> Coordinate:
        group, name = _split_module(raw.raw_id)
        return Coordinate(
            ecosystem=Ecosystem.GO,
            group=group,
            name=name,
            version_spec=raw.version_spec,
        )

    def publishes(self, path: Path) -> Coordinate | None:
        for _lineno, line, _indirect in _lines(path):
            directive, _, rest = line.partition(" ")
            if directive == "module" and rest.strip():
                group, name = _split_module(rest.strip().strip('"'))
                return Coordinate(ecosystem=Ecosystem.GO, group=group, name=name)
        return None


def _lines(path: Path) -> list[tuple[int, str, bool]]:
    """`(lineno, comment-free text, had-an-indirect-marker)` for every non-blank line."""
    out: list[tuple[int, str, bool]] = []
    for lineno, raw_line in enumerate(read_text(path).splitlines(), start=1):
        code, sep, comment = raw_line.partition("//")
        text = code.strip()
        if not text:
            continue
        out.append((lineno, text, bool(sep) and "indirect" in comment))
    return out


def _requirement(text: str, *, path: Path, lineno: int, indirect: bool) -> RawDependency:
    parts = text.split()
    if len(parts) != 2:
        raise ManifestParseError(
            f"{path}: line {lineno}: expected '<module path> <version>', got {text!r}"
        )
    module, version = parts
    if not version.startswith("v"):
        raise ManifestParseError(
            f"{path}: line {lineno}: {version!r} is not a Go semantic version for {module!r}"
        )
    return RawDependency(
        raw_id=module,
        version_spec=version,
        scope="indirect" if indirect else "direct",
        source_line=lineno,
    )


def _split_module(module: str) -> tuple[str, str]:
    """`github.com/acme/widget/v2` -> (`github.com/acme`, `widget/v2`).

    Group is the host + org (§5.2's "go host+org"); the remainder — including any `/vN` major
    suffix, which Go treats as a distinct module — is the name.
    """
    segments = module.split("/")
    if len(segments) <= 1:
        return "", module
    if len(segments) == 2:
        return segments[0], segments[1]
    return "/".join(segments[:2]), "/".join(segments[2:])
