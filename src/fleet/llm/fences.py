"""ADR-0146 — file-content fencing shared between `llm/calls.py::render_prompt` (the writer) and
`llm/backends/harmony_gpt_oss.py::_extract_pre_images` (the one reader). One module so the two can
never drift out of sync: `render_prompt` calls `fence_file`, `_extract_pre_images` calls
`trusted_fenced_blocks`, and neither side ever reimplements the other's half of the format.

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

**Injection safety (ADR-0146, required not optional) — two genuinely different mechanisms for two
genuinely different callers, corrected after an independent review found the original single
mechanism exploitable.**

1. Block discovery is SEQUENTIAL and non-overlapping (`_scan`): the scan consumes each block's full
   span — header through closing fence — before searching for the next header, so a forged header
   embedded inside one block's own content (a REAL file's content can itself contain text shaped
   like a fenced block — this repo's own source, or a Markdown file, is exactly such content) is
   never independently discovered as a second, top-level block. This layer alone is NOT sufficient
   by itself against attacker-supplied *whole messages* (see 2 below) — it only protects against a
   forgery nested inside a single already-legitimate block's own content.
2. `parse_file_fences(text, allowed_paths)` additionally checks every discovered path against a
   caller-supplied `allowed_paths` — never accepted merely because it parses. This is the right
   primitive for a caller that has an INDEPENDENT source of truth for which paths are legitimate,
   sourced from somewhere other than `text` itself; deriving `allowed_paths` by re-scanning the
   very same `text` (e.g. `parse_file_fences(text, discover_fenced_paths(text))`) restricts
   nothing — any fence found in `text` trivially allows itself — and an early version of this
   module's one production caller did exactly that, which an independent review correctly flagged
   as no restriction at all.

`harmony_gpt_oss.py::_extract_pre_images` has no independent per-path allowlist — `invoke()`
carries only `messages`, never a separate path list (`ModelBackend`'s fixed Protocol signature,
ADR-0146's rejected typed-side-channel alternative) — so it does not use `parse_file_fences` at
all. Instead it uses `trusted_fenced_blocks` below, restricted to `system`/`user`-role messages:
the real trust boundary is not "which paths does the text claim" but "who wrote this message". A
`system`/`user` message is ALWAYS 100% harness-authored text (`render_prompt()`'s own output,
`client.py`'s system-folding, or its repair-instruction template) — the model never writes into
one — so every top-level block layer 1's sequential scan finds inside such a message is
legitimate by construction, with no separate allowlist needed. `assistant`/`tool`-role messages
(which carry the model's own prior reply during a repair turn, `client.py::_repair_turns`) are
excluded from the scan entirely and unconditionally, by role, not by content — a forged fence
echoed back in the model's own reply is never scanned at all, closing the exploit an independent
review demonstrated: a forged `` ```path:<real path>\n<forged content>``` `` block in an
`assistant` message no longer overrides the real pre-image, and a path the harness never rendered
can no longer be introduced this way.

Even a forged pre-image that got through every layer here could only ever feed
`vcs/apply_patch.py::text_to_patch`/`commit_to_file_edits` a wrong pre-image for a real path.
`git apply` remains the harness's sole worktree writer (`vcs/git.py:435`), so a wrong pre-image
produces a diff that fails to apply — a safe, loud failure, not a silent one (disclosed, not a new
mechanism this module adds).
"""

from __future__ import annotations

import re
from collections.abc import Collection

__all__ = [
    "discover_fenced_paths",
    "fence_file",
    "parse_file_fences",
    "trusted_fenced_blocks",
]

_BACKTICK_RUN_RE = re.compile(r"`+")
_HEADER_RE = re.compile(r"^(?P<fence>`{3,})path:(?P<path>[^\n]*)\n", re.MULTILINE)


def _longest_backtick_run(content: str) -> int:
    return max((len(run) for run in _BACKTICK_RUN_RE.findall(content)), default=0)


def _fence_for(content: str) -> str:
    return "`" * max(_longest_backtick_run(content) + 1, 3)


def fence_file(path: str, content: str) -> str:
    """`content` — the literal on-disk text of `path` — as one raw fenced block. `path` must not
    contain a newline (every real caller's `path` is a repo-relative file path, never carrying
    one) — enforced, not merely assumed: a path containing `"\\n"` would let the caller's own path
    argument open a bogus second header line (e.g. `path="a\\n```path:b"`), silently producing a
    malformed block, so this raises loudly instead. Exact round trip:
    `parse_file_fences(fence_file(path, content), {path}) == {path: content}`, for any `content`
    and any single-line `path` (module docstring)."""
    if "\n" in path:
        raise ValueError(f"fence_file: path must not contain a newline: {path!r}")
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
    with no allowlist filtering. Feeding this straight back into `parse_file_fences(text, ...)` as
    that same call's `allowed_paths` restricts nothing (module docstring) — this function exists
    for a caller that combines it with an allowlist sourced from somewhere else."""
    return frozenset(path for path, _content in _scan(text))


def trusted_fenced_blocks(text: str) -> dict[str, str]:
    """One SEQUENTIAL, non-overlapping scan of `text` (`_scan`), trusting every top-level block it
    finds — for a caller that already knows, independently of anything `text` itself claims, that
    `text` is entirely harness-authored (module docstring: `harmony_gpt_oss.py::
    _extract_pre_images`, restricted to `system`/`user`-role messages, which is what makes this
    safe). There is no separate `allowed_paths` parameter here because there is nothing to check
    it against — layer 1 (nested-forgery protection) still applies, layer 2 (`parse_file_fences`'s
    allowlist) does not apply and is not needed, since the ONLY writer of these fences,
    `render_prompt`, only ever appears in exactly the kind of message this function is meant to be
    called on. Do not call this on text that could carry model output."""
    return dict(_scan(text))
