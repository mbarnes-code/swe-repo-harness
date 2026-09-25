"""ADR-0146 — file-content fencing shared between `llm/calls.py::render_prompt` (the writer) and
`llm/backends/harmony_gpt_oss.py::_extract_pre_images` (the one reader). One module so the two can
never drift out of sync: `render_prompt` calls `fence_file`, `_extract_pre_images` calls
`parse_file_fences`, and neither side ever reimplements the other's half of the format.

**Why file content is fenced raw instead of carried inside the JSON evidence blob.** `render_prompt`
serialises evidence with `ensure_ascii=True`, so a JSON string value holds *escaped* text — a
literal two-character `\\n`, not a real newline byte. A V4A/unified-diff hunk's context lines must
match the real file byte-for-byte, so a model reading escaped JSON has to mentally un-escape before
it can write a matching context line. A raw fenced block puts the real bytes in front of the model
instead (ADR-0146).

**Fence choice follows CommonMark's own fenced-code-block rule**: the fence is a run of backticks
one longer than the longest backtick run anywhere inside `content`, minimum 3 — so no backtick run
occurring naturally inside real file content (source code, Markdown, a docstring quoting a fence)
can ever be mistaken for the delimiter.

**Round trip is exact, not best-effort.** `fence_file` always inserts exactly one separator newline
between `content` and the closing fence line; `parse_file_fences` locates the close by a literal
`"\\n" + fence` substring search rather than by guessing where "the end of content" is from
`content`'s own trailing-newline state. Because the fence is longer than any backtick run genuinely
inside `content`, that exact substring cannot occur anywhere inside `content` itself, so the first
occurrence found after the header is always the true close. This holds for content containing CRLF,
no trailing newline, a trailing blank line, embedded backtick runs shorter than the fence, and
non-ASCII UTF-8 text.

**Injection safety (ADR-0146, required not optional).** `parse_file_fences` never infers which
paths are legitimate from the text itself — a REAL file's content can itself contain text shaped
like a fenced block (this repo's own source, or a Markdown file, is exactly such content), naming
an arbitrary path. Two independent layers close this:

1. Block discovery is SEQUENTIAL and non-overlapping (`_scan`): the scan consumes each block's full
   span — header through closing fence — before searching for the next header, so a forged header
   embedded inside one block's own content is never independently discovered as a second,
   top-level block.
2. Every discovered path is additionally checked against the caller-supplied `allowed_paths` —
   never accepted merely because it parses.

A caller with an independent source of truth for which paths are legitimate gets real protection
from layer 2. `harmony_gpt_oss.py::_extract_pre_images` has no such independent source — `invoke()`
carries only `messages`, never a separate path list (`ModelBackend`'s fixed Protocol signature,
ADR-0146's rejected typed-side-channel alternative) — so it derives its own allowlist via
`discover_fenced_paths`, the same sequential scan, and so gets protection from layer 1 alone for
that one caller; this is disclosed here rather than overstated.

Even a forged pre-image that gets through both layers can only ever feed
`vcs/apply_patch.py::text_to_patch`/`commit_to_file_edits` a wrong pre-image for a real path.
`git apply` remains the harness's sole worktree writer (`vcs/git.py:435`), so a wrong pre-image
produces a diff that fails to apply — a safe, loud failure, not a silent one (disclosed, not a new
mechanism this module adds).
"""

from __future__ import annotations

import re
from collections.abc import Collection

__all__ = ["discover_fenced_paths", "fence_file", "parse_file_fences"]

_BACKTICK_RUN_RE = re.compile(r"`+")
_HEADER_RE = re.compile(r"^(?P<fence>`{3,})path:(?P<path>[^\n]*)\n", re.MULTILINE)


def _longest_backtick_run(content: str) -> int:
    return max((len(run) for run in _BACKTICK_RUN_RE.findall(content)), default=0)


def _fence_for(content: str) -> str:
    return "`" * max(_longest_backtick_run(content) + 1, 3)


def fence_file(path: str, content: str) -> str:
    """`content` — the literal on-disk text of `path` — as one raw fenced block. `path` must not
    contain a newline (every real caller's `path` is a repo-relative file path, never carrying
    one). Exact round trip: `parse_file_fences(fence_file(path, content), {path}) ==
    {path: content}`, for any `content` and any single-line `path` (module docstring)."""
    fence = _fence_for(content)
    return f"{fence}path:{path}\n{content}\n{fence}"


def _scan(text: str) -> list[tuple[str, str]]:
    """`(path, content)` for every well-formed fenced block in `text`, in the order found, by a
    SEQUENTIAL non-overlapping scan (module docstring, injection-safety layer 1). A header with no
    matching close is skipped — resuming the scan right after that lone header, not after the rest
    of the text, so a genuinely malformed prefix cannot hide a real block that follows it."""
    blocks: list[tuple[str, str]] = []
    pos = 0
    while True:
        match = _HEADER_RE.search(text, pos)
        if match is None:
            break
        fence = match.group("fence")
        path = match.group("path")
        content_start = match.end()
        closer = f"\n{fence}"
        close_idx = text.find(closer, content_start)
        while close_idx != -1:
            after = close_idx + len(closer)
            if after == len(text) or text[after] == "\n":
                break
            close_idx = text.find(closer, after)
        if close_idx == -1:
            pos = match.end()
            continue
        blocks.append((path, text[content_start:close_idx]))
        pos = close_idx + len(closer)
    return blocks


def parse_file_fences(text: str, allowed_paths: Collection[str]) -> dict[str, str]:
    """The inverse of `fence_file`, restricted to `allowed_paths` (module docstring: injection
    safety, layer 2). A path discovered by `_scan` but absent from `allowed_paths` is dropped, not
    reported — the caller's allowlist is the only source of truth for which paths are legitimate,
    never text found in `text` itself."""
    allowed = frozenset(allowed_paths)
    return {path: content for path, content in _scan(text) if path in allowed}


def discover_fenced_paths(text: str) -> frozenset[str]:
    """Every path a SEQUENTIAL, non-overlapping scan of `text` finds (`_scan`) — `parse_file_fences`
    with no allowlist filtering. The one legitimate use is a caller with no independent source of
    truth for which paths it rendered (module docstring's injection-safety note on
    `harmony_gpt_oss.py::_extract_pre_images`, the one caller that uses this instead of a real
    allowlist)."""
    return frozenset(path for path, _content in _scan(text))
