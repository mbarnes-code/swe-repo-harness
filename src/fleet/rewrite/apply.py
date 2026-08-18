"""Unified-diff construction, validation, in-memory application, and `git apply` (§3.2 step 6).

Four responsibilities, all of them about *one* representation — the unified diff — because
`FilePatch.diff` is the only accepted patch representation (§5.4):

* **Construct** (`make_unified_diff`): the pipeline's single per-file patch, built once against
  the file's committed text.
* **Apply in memory** (`apply_in_memory`): the pipeline threads text, but §7.4 pins
  `Rewriter.apply` to return a `FilePatch`, so the pipeline has to turn an engine's diff back
  into text. Doing it here rather than in the pipeline means the *same* parser validates what an
  engine produced and what `git apply` will later be asked to swallow: a driver that emits a
  hunk whose context does not match the buffer it was handed fails loudly at the seam that
  produced it, instead of at `git apply` three phases later with no rule id attached.
* **Validate** (`check_diff` / `validate_diff`): reject any hunk whose path escapes the repo's
  own subtree, *before* the mutation is journalled, so an out-of-tree write is never even
  intended (§3.2 step 6.6). `check_diff` also cross-checks a caller-supplied declared path (a
  `FilePatch.path`) against the diff's own header paths, because that field and the diff can
  originate as two independently model-supplied values with nothing else forcing them to agree.
* **Write** (`apply_patch`): `git apply` is the ONLY writer of source files — no worker opens a
  file for writing.

Nothing here holds a rule, an engine, or a buffer; the pipeline owns those (§7.4).
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from fleet.models.tasks import FilePatch
from fleet.util.fs import scoped_tempdir
from fleet.vcs.git import Git, GitCommandError

__all__ = [
    "NO_NEWLINE_MARKER",
    "ApplyResult",
    "DiffFormatError",
    "FileDiff",
    "Hunk",
    "PatchApplyError",
    "apply_in_memory",
    "apply_patch",
    "check_diff",
    "diff_paths",
    "make_unified_diff",
    "parse_unified_diff",
    "validate_diff",
]

NO_NEWLINE_MARKER: Final = "\\ No newline at end of file"
"""git's spelling. Emitted and understood, because dropping it silently appends a newline to a
file that had none — a one-byte content change no reviewer asked for and no rule authored."""

_HUNK_HEADER: Final = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class DiffFormatError(ValueError):
    """The text is not a unified diff this module can parse. Loud, never a silent empty parse."""


class PatchApplyError(RuntimeError):
    """A hunk's context did not match the text it was applied to (Rule 11).

    Raised by `apply_in_memory` only. The `git apply` path answers the same question with an exit
    code, which `apply_patch` reports as a rejection reason rather than an exception.
    """


@dataclass(frozen=True, slots=True)
class Hunk:
    """One `@@` block. Line numbers are 1-based as the format writes them; `old_span()` converts
    to the 0-based half-open range the pipeline reasons about."""

    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: tuple[str, ...]

    def old_span(self) -> tuple[int, int]:
        """0-based half-open `[start, end)` over the PRE-image lines this hunk consumes.

        A pure insertion has `old_len == 0`, and git writes its `old_start` as the line *before*
        the insertion point, so the zero-width span sits at `old_start` — not `old_start - 1`.
        """
        if self.old_len == 0:
            return (self.old_start, self.old_start)
        return (self.old_start - 1, self.old_start - 1 + self.old_len)


@dataclass(frozen=True, slots=True)
class FileDiff:
    """One file's worth of hunks, with the paths the `---`/`+++` headers named."""

    old_path: str
    new_path: str
    hunks: tuple[Hunk, ...]

    @property
    def path(self) -> str:
        """The post-image path — what a validator must bound, since that is what gets written."""
        return self.new_path if self.new_path != "/dev/null" else self.old_path


def _strip_prefix(header: str) -> str:
    """`--- a/src/x.py` → `src/x.py`. `/dev/null` and prefix-less paths pass through unchanged."""
    path = header.split("\t", 1)[0].strip()
    if path == "/dev/null":
        return path
    if len(path) > 2 and path[1] == "/" and path[0] in {"a", "b", "i", "w", "c", "o"}:
        return path[2:]
    return path


def make_unified_diff(path: str, before: str, after: str, *, context: int = 3) -> str:
    """A git-applicable unified diff for one file, or `""` when the texts are identical.

    `""` rather than a header-only diff: an empty change must be *falsy*, so "did this rule do
    anything" is one truth test and never a parse.
    """
    if before == after:
        return ""
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    out: list[str] = []
    for line in difflib.unified_diff(
        before_lines, after_lines, fromfile=f"a/{path}", tofile=f"b/{path}", n=context
    ):
        out.append(line if line.endswith("\n") else line + "\n")
        if line and not line.endswith("\n") and not line.startswith(("---", "+++", "@@")):
            out.append(NO_NEWLINE_MARKER + "\n")
    return "".join(out)


def parse_unified_diff(diff: str) -> tuple[FileDiff, ...]:
    """Parse a (possibly multi-file) unified diff. Tolerates `diff --git` and `index` preamble
    lines, which git emits and `difflib` does not."""
    files: list[FileDiff] = []
    old_path: str | None = None
    new_path: str | None = None
    hunks: list[Hunk] = []
    current: list[str] | None = None
    header: tuple[int, int, int, int] | None = None

    def flush_hunk() -> None:
        nonlocal current, header
        if current is not None and header is not None:
            hunks.append(Hunk(header[0], header[1], header[2], header[3], tuple(current)))
        current, header = None, None

    def flush_file() -> None:
        nonlocal old_path, new_path, hunks
        flush_hunk()
        if old_path is not None and new_path is not None:
            files.append(FileDiff(old_path, new_path, tuple(hunks)))
        old_path, new_path, hunks = None, None, []

    for raw in diff.splitlines():
        if raw.startswith("--- "):
            flush_file()
            old_path = _strip_prefix(raw[4:])
        elif raw.startswith("+++ "):
            if old_path is None:
                raise DiffFormatError("`+++` header with no preceding `---` header")
            new_path = _strip_prefix(raw[4:])
        elif raw.startswith("@@"):
            match = _HUNK_HEADER.match(raw)
            if match is None:
                raise DiffFormatError(f"unparseable hunk header: {raw!r}")
            if new_path is None:
                raise DiffFormatError(f"hunk before any file header: {raw!r}")
            flush_hunk()
            header = (
                int(match.group(1)),
                1 if match.group(2) is None else int(match.group(2)),
                int(match.group(3)),
                1 if match.group(4) is None else int(match.group(4)),
            )
            current = []
        elif current is not None and (raw[:1] in {" ", "+", "-", "\\"} or raw == ""):
            # A context line that is genuinely empty loses its leading space in some producers;
            # treat it as such rather than ending the hunk early on a blank line.
            current.append(raw if raw else " ")
    flush_file()
    if not files:
        raise DiffFormatError("no `---`/`+++` file headers found")
    return tuple(files)


def _hunk_payload(lines: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split a hunk body into (pre-image lines, post-image lines), each newline-terminated except
    where a `\\ No newline at end of file` marker says otherwise."""
    pre: list[str] = []
    post: list[str] = []
    for index, line in enumerate(lines):
        if line.startswith("\\"):
            continue
        no_newline = index + 1 < len(lines) and lines[index + 1].startswith("\\")
        body = line[1:] + ("" if no_newline else "\n")
        if line.startswith(" "):
            pre.append(body)
            post.append(body)
        elif line.startswith("-"):
            pre.append(body)
        elif line.startswith("+"):
            post.append(body)
    return pre, post


def apply_in_memory(text: str, diff: str) -> str:
    """Apply a single-file unified diff to `text` and return the result.

    Context and removal lines must match EXACTLY — no fuzz, no offset search. Fuzzy application
    is how a patch lands in the wrong place and still exits zero; here a mismatch is a
    `PatchApplyError` naming the line, because the only caller is the pipeline checking an
    engine's own output against the buffer it just handed that engine.
    """
    files = parse_unified_diff(diff)
    if len(files) != 1:
        raise PatchApplyError(f"expected a single-file diff, got {len(files)} file sections")
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    cursor = 0
    for hunk in sorted(files[0].hunks, key=lambda h: h.old_start):
        pre, post = _hunk_payload(hunk.lines)
        start = hunk.old_start - 1 if hunk.old_len else hunk.old_start
        if start < cursor:
            raise PatchApplyError(
                f"hunk at old line {hunk.old_start} overlaps an earlier hunk in the same diff"
            )
        if start + len(pre) > len(lines):
            raise PatchApplyError(
                f"hunk at old line {hunk.old_start} runs past the end of a {len(lines)}-line file"
            )
        actual = lines[start : start + len(pre)]
        if actual != pre:
            raise PatchApplyError(
                f"hunk at old line {hunk.old_start} does not match: expected {pre!r}, "
                f"found {actual!r}"
            )
        out.extend(lines[cursor:start])
        out.extend(post)
        cursor = start + len(pre)
    out.extend(lines[cursor:])
    return "".join(out)


def diff_paths(diff: str) -> tuple[str, ...]:
    """Every path a diff would write, post-image, in file order."""
    return tuple(f.path for f in parse_unified_diff(diff))


def _escapes(path: str, subtree: str) -> bool:
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts:
        return True
    if not subtree:
        return False
    root = PurePosixPath(subtree)
    return not pure.is_relative_to(root)


def check_diff(
    diff: str,
    dest_subtree: str,
    *,
    max_bytes: int | None = None,
    allow_paths_outside_dest: bool = False,
    declared_path: str | None = None,
) -> str | None:
    """`None` when the diff may be applied, else a one-line reason it may not.

    A reason string rather than a bare `False`: "patch rejected" with no cause is exactly the
    log line that makes a Phase-2 outage unattributable to the rule that caused it.

    `declared_path`, when given, is a `FilePatch.path` (or equivalent) that must agree with the
    diff it travels with: `declared_path in diff_paths(diff)`. `FilePatch.path` and `FilePatch.diff`
    can originate as two independently model-supplied fields (`ProposedFileEdit`), so nothing
    upstream of this guarantees they name the same file — `git apply` only ever looks at the
    diff's own `---`/`+++` headers, so a mismatched `declared_path` is the file a downstream parse
    probe checks while a *different* file is the one that was actually written. Membership, not
    equality, because one `FilePatch` may legitimately carry a diff touching several files, and
    for a rename `diff_paths` reports only the post-image (destination) path — the file that ends
    up on disk — never the pre-image one, so a legitimate rename's declared destination path is
    never rejected here.
    """
    if max_bytes is not None and len(diff.encode("utf-8")) > max_bytes:
        return f"patch is larger than transform.max_patch_bytes ({max_bytes} bytes)"
    try:
        paths = diff_paths(diff)
    except DiffFormatError as exc:
        return f"not a parseable unified diff: {exc}"
    if not paths:
        return "diff names no files"
    if declared_path is not None and declared_path not in paths:
        return (
            f"declared path {declared_path!r} does not match any path its own diff writes "
            f"{list(paths)!r}"
        )
    if allow_paths_outside_dest:
        return None
    outside = sorted(p for p in paths if _escapes(p, dest_subtree))
    if outside:
        return f"hunk paths escape the repo subtree {dest_subtree!r}: {outside}"
    return None


async def validate_diff(diff: str, dest_subtree: str) -> bool:
    """Reject any hunk whose path escapes `dest_subtree`. Runs before the journal write."""
    return check_diff(diff, dest_subtree) is None


def _terminated(diff: str) -> str:
    """Restore the trailing newline `FilePatch` ate.

    `FleetModel` sets `str_strip_whitespace=True` (§5), so the moment a diff becomes a
    `FilePatch.diff` it loses its final `\\n`. `git apply` then rejects the whole thing with
    "corrupt patch at line N" — a valid patch, refused for a reason that has nothing to do with
    the rewrite, which would look exactly like a rule defect and cost an escalation rung.
    """
    return diff if diff.endswith("\n") else diff + "\n"


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """What happened when a patch met the worktree. `reason` is set iff `ok` is False."""

    ok: bool
    path: str
    reason: str | None = None
    parse_probe_ok: bool = False
    already_applied: bool = False


async def apply_patch(
    worktree: str | Path,
    patch: FilePatch,
    *,
    dest_subtree: str = "",
    git: Git | None = None,
    max_bytes: int | None = None,
    allow_paths_outside_dest: bool = False,
    probe: Callable[[str], Awaitable[bool]] | None = None,
) -> ApplyResult:
    """Validate, `git apply --check`, `git apply`, then re-probe. The only writer (§3.2 step 6.6).

    `git apply --check --reverse` is consulted first: a patch whose effect is already present is
    reported `already_applied` rather than re-applied, which is the §11.7 idempotency contract for
    a partial re-run.
    """
    reason = check_diff(
        patch.diff,
        dest_subtree,
        max_bytes=max_bytes,
        allow_paths_outside_dest=allow_paths_outside_dest,
        declared_path=patch.path,
    )
    if reason is not None:
        return ApplyResult(ok=False, path=patch.path, reason=reason)
    repo = Git(worktree) if git is None else git
    with scoped_tempdir(prefix="fleet-patch-") as tmp:
        patch_file = tmp / "patch.diff"
        patch_file.write_text(_terminated(patch.diff), encoding="utf-8")
        if await repo.apply_check(patch_file, reverse=True):
            return ApplyResult(ok=True, path=patch.path, already_applied=True)
        if not await repo.apply_check(patch_file):
            return ApplyResult(
                ok=False, path=patch.path, reason="`git apply --check` refused the patch"
            )
        try:
            await repo.apply(patch_file)
        except GitCommandError as exc:
            return ApplyResult(ok=False, path=patch.path, reason=f"`git apply` failed: {exc}")
    # The probe asks "does the file that was just written still parse", so it has to be pointed at
    # THAT file. `patch.path` is repo-relative — `check_diff` has already proven it stays inside
    # `dest_subtree`, which is itself a path within the worktree — and a probe resolves a relative
    # path against the process cwd, which is not the worktree except by accident. `repo.path`
    # rather than `worktree` because an injected `git` is the handle that actually wrote the file.
    probe_ok = True if probe is None else await probe(str(repo.path / patch.path))
    if not probe_ok:
        return ApplyResult(
            ok=False, path=patch.path, reason="parse probe failed after apply", parse_probe_ok=False
        )
    return ApplyResult(ok=True, path=patch.path, parse_probe_ok=True)
