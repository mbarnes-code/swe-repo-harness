"""ADR-0021, §3.2 step 5: `diff → ApproachElement tuples → approach_signature`.

The whole point is that "the same idea, retyped" must fingerprint identically. A stubbed LLM
that returns the same fix twice — re-indented, re-ordered, with shifted hunk offsets the second
time — must collide; a genuinely different fix must not. That guarantee comes from throwing away
everything a diff carries that is NOT about the approach:

* line offsets and context-line counts (`@@ -a,b +c,d @@` is never read here past locating hunks),
* hunk order (the element set is sorted before hashing),
* formatting/whitespace (a hunk whose only change is whitespace is discarded outright, and the
  surviving classification is structural — via `ast-grep` — never a raw-text comparison).

**Agent Recommendation (CLAUDE.md guardrail 1) — the vocabulary is a deliberate simplification.**
`docs/SPEC.md:899-917` (ADR-0021 / §3.2 step 5) names a closed `change_kind` vocabulary
(`IMPORT_REWRITE`, `PACKAGE_DECL`, `PATH_ALIAS`, `SYMBOL_RENAME`, `DEP_ADD`, `DEP_REMOVE`,
`FILE_ADD`, `FILE_DELETE`, `OTHER`) and a `target_symbol` sourced from `symbols.fqn` — the
project's own indexed symbol table. This module has no database access (it is a pure
diff-in/signature-out function, callable with nothing but a list of unified diffs), so:

* `target_symbol` is approximated by scanning the hunk's OWN lines (the diff's `@@` context plus
  the changed lines) for the nearest preceding `def`/`class`/`function`/`func` signature, rather
  than a real `symbols.fqn` lookup. This is a strictly weaker signal than the real symbol table —
  a hunk with no such line in its own (3-line, by default) context reports `""` even when a real
  symbol index would resolve one — and is disclosed here as the load-bearing simplification a
  reviewer should look at first.
* `PATH_ALIAS` and `SYMBOL_RENAME` are not independently detected (both would need real
  before/after AST diffing, not per-hunk classification) and fold into `OTHER`. `DEP_ADD`/
  `DEP_REMOVE` are broadened from "a dependency manifest line changed" to "the hunk is pure
  insertion / pure deletion with no ast-grep-recognised import or package construct" — a
  reasonable stand-in that costs nothing in the collision guarantee (a pure add/remove is still
  offset/whitespace/order-insensitive) and is documented rather than silently narrowed.
* `IMPORT_REWRITE`/`PACKAGE_DECL` ARE real `ast-grep` structural classifications (`kind:`-based
  inline rules against the hunk's post-image, or pre-image for a pure deletion), for the
  languages in `_IMPORT_KINDS`/`_PACKAGE_KINDS` below. An unmapped language, or `ast-grep` not
  being on `PATH`, degrades that ONE hunk to the add/remove/OTHER heuristic rather than raising —
  unlike `rewrite/astgrep.py`'s rewrite engine (where an absent tool is a hard error because it is
  the only thing standing between a rule and a file), a fingerprinting helper that raised on a
  language it cannot classify would make anchoring detection itself unavailable for every OTHER
  language too. The collision guarantee does not depend on `change_kind` accuracy: two
  identically-reformatted diffs produce identical elements regardless of which bucket a hunk
  lands in, because the bucket is a deterministic function of structure, never of raw text.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Final

import yaml  # type: ignore[import-untyped]  # types-PyYAML is not a dependency

from fleet.rewrite.apply import Hunk, _hunk_payload, parse_unified_diff
from fleet.rewrite.rules import language_for_path
from fleet.util.fs import scoped_tempdir
from fleet.util.proc import CommandRunner, run

__all__ = [
    "ApproachElement",
    "ChangeKind",
    "compute_approach_signature",
]


class ChangeKind(StrEnum):
    """The closed vocabulary `docs/SPEC.md:906-909` names, per surviving hunk."""

    IMPORT_REWRITE = "IMPORT_REWRITE"
    PACKAGE_DECL = "PACKAGE_DECL"
    PATH_ALIAS = "PATH_ALIAS"
    SYMBOL_RENAME = "SYMBOL_RENAME"
    DEP_ADD = "DEP_ADD"
    DEP_REMOVE = "DEP_REMOVE"
    FILE_ADD = "FILE_ADD"
    FILE_DELETE = "FILE_DELETE"
    OTHER = "OTHER"


@dataclass(frozen=True, slots=True)
class ApproachElement:
    """One surviving hunk, normalized. `docs/SPEC.md:905`: `(path, change_kind, target_symbol)`."""

    path: str
    change_kind: ChangeKind
    target_symbol: str


# -- ast-grep structural classification (Agent Recommendation; see module docstring) -----------

_IMPORT_KINDS: Final[Mapping[str, tuple[str, ...]]] = {
    "python": ("import_statement", "import_from_statement"),
    "typescript": ("import_statement",),
    "tsx": ("import_statement",),
    "javascript": ("import_statement",),
    "jsx": ("import_statement",),
    "go": ("import_declaration",),
    "java": ("import_declaration",),
    "rust": ("use_declaration",),
}

_PACKAGE_KINDS: Final[Mapping[str, tuple[str, ...]]] = {
    "go": ("package_clause",),
    "java": ("package_declaration",),
    "scala": ("package_clause",),
}

_DEF_LINE: Final = re.compile(
    r"^[+\- ]?\s*(?:async\s+)?(?:def|class|function|func)\s+([A-Za-z_][A-Za-z0-9_]*)"
)


def _target_symbol(hunk: Hunk) -> str:
    """The nearest preceding `def`/`class`/`function`/`func` name in the hunk's OWN lines.

    A text heuristic, not a `symbols.fqn` lookup — see the module docstring's disclosure. Scans
    every line (context and changed alike, in order) and keeps the LAST match seen; a hunk whose
    3-line default context never shows an enclosing signature reports `""`, exactly as
    `docs/SPEC.md:909` says a hunk "not inside one" should.
    """
    candidate = ""
    for line in hunk.lines:
        match = _DEF_LINE.match(line)
        if match:
            candidate = match.group(1)
    return candidate


def _is_whitespace_only(hunk: Hunk) -> bool:
    """`docs/SPEC.md:900`: "Discard hunks whose change is whitespace-only." Token-stream equal
    pre/post, ignoring how much whitespace separates the tokens or where the line breaks fall."""
    pre, post = _hunk_payload(hunk.lines)
    return "".join(pre).split() == "".join(post).split()


async def _classify(
    hunk: Hunk,
    path: str,
    *,
    binary: str,
    runner: CommandRunner,
    timeout_s: float,
) -> ChangeKind:
    """`docs/SPEC.md:907-908`'s closed vocabulary, structural where `ast-grep` can tell and
    diff-structural (pure add / pure remove) otherwise."""
    pre, post = _hunk_payload(hunk.lines)
    # Classify the POST image when there is one (the result of the change); a pure removal has
    # none, so classify what is being removed instead.
    text = "".join(post) if post else "".join(pre)
    language = language_for_path(path)
    if language is not None and text.strip():
        kind = await _ast_grep_kind(
            text, path, language, binary=binary, runner=runner, timeout_s=timeout_s
        )
        if kind is not None:
            return kind
    if not post:  # pure removal
        return ChangeKind.DEP_REMOVE
    if not pre:  # pure insertion
        return ChangeKind.DEP_ADD
    return ChangeKind.OTHER


async def _ast_grep_kind(
    text: str,
    path: str,
    language: str,
    *,
    binary: str,
    runner: CommandRunner,
    timeout_s: float,
) -> ChangeKind | None:
    """`None` when `ast-grep` is unavailable, the snippet fails to error-free parse a match, or
    the language is not in either kind map — the caller then falls back to the add/remove/OTHER
    heuristic (see module docstring)."""
    import_kinds = _IMPORT_KINDS.get(language)
    package_kinds = _PACKAGE_KINDS.get(language)
    if import_kinds is None and package_kinds is None:
        return None
    with scoped_tempdir(prefix="fleet-approach-") as tmp:
        target = tmp / f"snippet{PurePosixPath(path).suffix or '.txt'}"
        target.write_text(text, encoding="utf-8")
        if import_kinds is not None and await _matches_any_kind(
            target, language, import_kinds, binary=binary, runner=runner, timeout_s=timeout_s
        ):
            return ChangeKind.IMPORT_REWRITE
        if package_kinds is not None and await _matches_any_kind(
            target, language, package_kinds, binary=binary, runner=runner, timeout_s=timeout_s
        ):
            return ChangeKind.PACKAGE_DECL
    return None


async def _matches_any_kind(
    target: Path,
    language: str,
    kinds: tuple[str, ...],
    *,
    binary: str,
    runner: CommandRunner,
    timeout_s: float,
) -> bool:
    document = yaml.safe_dump(
        {
            "id": "fleet-approach-classify",
            "language": language,
            "rule": {"any": [{"kind": kind} for kind in kinds]},
        },
        sort_keys=True,
    )
    argv = [binary, "scan", "--inline-rules", document, "--json", str(target)]
    try:
        result = await runner(argv, timeout_s=timeout_s)
    except FileNotFoundError:
        return False  # ast-grep not on PATH: degrade to the structural fallback, never raise
    if not result.started or result.timed_out:
        return False
    stripped = result.stdout_tail.strip()
    return bool(stripped) and stripped not in ("[]", "null")


# -- the public entry point ----------------------------------------------------------------


async def compute_approach_signature(
    diffs: Sequence[str],
    *,
    binary: str = "ast-grep",
    runner: CommandRunner = run,
    timeout_s: float = 15.0,
) -> str:
    """`docs/SPEC.md:899-917`: one 64-hex fingerprint over EVERY diff a rung proposed.

    `diffs` is every `FilePatch.diff` a single repair candidate produced (a rung may touch more
    than one file for one unit; §3.2 step 6 still lands them as one commit, so they are one
    approach). Line offsets, context, hunk order and formatting never reach the signature: hunks
    are parsed structurally, whitespace-only hunks are discarded, and the surviving
    `ApproachElement`s are SORTED before hashing (`docs/SPEC.md:912`) — order in the input diffs,
    order of hunks within a file, and order of files in `diffs` are all irrelevant.
    """
    elements: list[ApproachElement] = []
    for diff in diffs:
        for file_diff in parse_unified_diff(diff):
            path = file_diff.path
            if file_diff.old_path == "/dev/null":
                elements.extend(
                    ApproachElement(path, ChangeKind.FILE_ADD, "") for _ in file_diff.hunks
                )
                continue
            if file_diff.new_path == "/dev/null":
                elements.extend(
                    ApproachElement(path, ChangeKind.FILE_DELETE, "") for _ in file_diff.hunks
                )
                continue
            for hunk in file_diff.hunks:
                if _is_whitespace_only(hunk):
                    continue
                kind = await _classify(
                    hunk, path, binary=binary, runner=runner, timeout_s=timeout_s
                )
                elements.append(ApproachElement(path, kind, _target_symbol(hunk)))
    joined = "\n".join(
        sorted(f"{e.path}\x1f{e.change_kind}\x1f{e.target_symbol}" for e in elements)
    )
    return sha256(joined.encode("utf-8")).hexdigest()
