"""Repo-wide guard: no RFC 1918 (private-range) IP literal in any tracked file.

Why this exists
----------------
This repo is public on GitHub. A sweep at commit `438e19d` with the same regex `_RFC1918_REGEX`
encodes below (see `docs/INTEGRATION_HONESTY.md` D149 for the exact `git grep` invocation)
found 19 hits: three placeholder Spark endpoints in `docs/SPEC.md` and their mirrors in
`tests/test_settings.py` (`10.0.0.5`/`10.0.0.6`), an unreachable "off-loopback" literal in
`tests/test_llm_openai_stub_server_e2e.py` that used a genuinely private address (`10.255.255.1`)
while its own docstring claimed TEST-NET-3, and — the one with real exposure — the operator's
actual LAN address (`192.168.1.12:3001`, a real Gitea host) paired with their actual Gitea
username (`redmage`) in a docstring and a test fixture in `src/fleet/vcs/gitea.py` and
`tests/test_gitea.py`. All 19 were replaced with RFC 5737 documentation-range addresses
(`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24` — see D149 in
`docs/INTEGRATION_HONESTY.md`), which read as unambiguously fake to any future reader and can
never resolve to a real host. This test is the mechanical backstop: it fails loud, naming
`file:line`, the moment a future edit reintroduces a real private-range literal.

Not addressed, and not addressable by this test: git history still contains the pre-fix commits
carrying the real address. Rewriting history was judged out of scope (an RFC 1918 LAN address is
not a secret — see D149). This test only guards the tracked-file *content* going forward.

Why this is mutation-testable (CLAUDE.md Rule 12)
---------------------------------------------------
The detection logic is one pure function, `_find_private_ip_hits`, taking the file's text as a
plain string and returning `(line_number, matched_literal)` pairs. The unit tests below run it
against literal fixture strings under this file's own control, so a mutation that narrows or
drops one of the three RFC 1918 branches (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`)
reddens a fixture built to discriminate exactly that branch — see the mutation matrix in this
task's report. The one integration test at the bottom then runs the same function against every
file `git ls-files` reports for the real repo.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

# The exact RFC 1918 (private-range) sweep regex used to find and fix all 19 pre-existing hits
# (see D149, docs/INTEGRATION_HONESTY.md). Encodes the three private ranges:
#   10.0.0.0/8        -> 10\.[0-9]{1,3}
#   172.16.0.0/12     -> 172\.(1[6-9]|2[0-9]|3[01])   (172.16 through 172.31 only)
#   192.168.0.0/16    -> 192\.168
# followed by two more dotted octets, word-bounded so it doesn't match inside a longer number.
_RFC1918_REGEX = re.compile(
    r"\b(10\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01])|192\.168)\.[0-9]{1,3}\.[0-9]{1,3}\b"
)

# Exact "path:line" allowlist for a hit that genuinely cannot be fixed. Left empty: every one of
# the 19 hits found at commit 438e19d was fixed (replaced with an RFC 5737 documentation-range
# address), so there is nothing to allowlist. Do not add an entry here without a comment
# explaining why the literal cannot simply be replaced.
_ALLOWLIST: frozenset[str] = frozenset()

# This file's own path, relative to the repo root, exactly as `git ls-files` prints it. Excluded
# from the sweep below by rule (not by line-level allowlist entries): the unit test fixtures just
# above deliberately contain literal RFC 1918 strings (e.g. `172.20.5.5`) to exercise each branch
# of `_RFC1918_REGEX` directly -- that is what makes the branches mutation-testable per Rule 12.
# Those literals are inert test data, never a real address, and the unit tests are themselves the
# check on them; the repo-wide sweep exists to catch a literal in *production* prose/config/code,
# not to forbid this file from quoting the very pattern it tests.
_SELF_PATH = "tests/test_no_private_addresses_in_tracked_files.py"


def _find_private_ip_hits(text: str) -> list[tuple[int, str]]:
    """Every RFC 1918 IP literal in `text`, as `(1-based line number, matched literal)` pairs.

    Pure function of the text alone -- no filesystem or git access -- so it can be exercised
    directly against fixture strings (see the unit tests below) as well as against real file
    contents (see the integration test at the bottom).
    """
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in _RFC1918_REGEX.finditer(line):
            hits.append((lineno, match.group(0)))
    return hits


# ---------------------------------------------------------------------------
# Unit tests against literal, hand-built fixtures (these are what make the logic
# mutation-testable -- see the module docstring and this task's mutation matrix).
# ---------------------------------------------------------------------------


def test_no_hits_for_a_clean_fixture() -> None:
    fixture = "base_url: 'http://192.0.2.5:8000/v1'\nowner = 'acme-owner'\n"
    assert _find_private_ip_hits(fixture) == []


def test_matches_the_10_range() -> None:
    """10.0.0.0/8 -- discriminates a mutation that drops this branch entirely."""
    fixture = "base_url: 'http://10.5.5.5:8000/v1'\n"
    assert _find_private_ip_hits(fixture) == [(1, "10.5.5.5")]


def test_matches_the_172_16_through_31_range() -> None:
    """172.16.0.0/12 -- discriminates a mutation that drops the `172\\.(1[6-9]|2[0-9]|3[01])`
    branch entirely."""
    fixture = "host = '172.20.5.5'\n"
    assert _find_private_ip_hits(fixture) == [(1, "172.20.5.5")]


def test_does_not_match_172_15_or_172_32_boundary_addresses() -> None:
    """172.15.x.x and 172.32.x.x are OUTSIDE RFC 1918 (the private block is only 172.16-172.31).
    A control on the 172 branch's boundary: a mutation that widened the branch (e.g. to
    `172\\.[0-9]{1,3}`) would wrongly match these and this test would catch it."""
    fixture = "a = '172.15.5.5'\nb = '172.32.5.5'\n"
    assert _find_private_ip_hits(fixture) == []


def test_matches_the_192_168_range_beyond_the_first_subnet() -> None:
    """192.168.0.0/16 -- uses a second octet other than 1, discriminating a mutation that
    narrows the branch to only `192\\.168\\.1\\.`."""
    fixture = "endpoint = '192.168.50.7'\n"
    assert _find_private_ip_hits(fixture) == [(1, "192.168.50.7")]


def test_does_not_match_a_public_ip_address() -> None:
    """Control: a public, routable address must never be flagged."""
    fixture = "dns = '8.8.8.8'\nanother = '203.0.113.1'\n"
    assert _find_private_ip_hits(fixture) == []


def test_reports_the_correct_line_number_for_a_hit_past_the_first_line() -> None:
    fixture = "line one, no hit\nline two, no hit\nline three has 10.1.2.3 in it\n"
    assert _find_private_ip_hits(fixture) == [(3, "10.1.2.3")]


def test_reports_every_hit_on_a_line_with_more_than_one() -> None:
    fixture = "base_urls: ['http://10.0.0.5/v1', 'http://10.0.0.6/v1']\n"
    assert _find_private_ip_hits(fixture) == [(1, "10.0.0.5"), (1, "10.0.0.6")]


def test_self_path_constant_matches_this_files_actual_repo_relative_path() -> None:
    """Guards the self-exclusion itself: if this file is ever moved, `_SELF_PATH` silently stops
    matching what `git ls-files` reports, and the sweep would flag this file's own fixtures."""
    actual = Path(__file__).resolve().relative_to(_REPO_ROOT).as_posix()
    assert actual == _SELF_PATH


# ---------------------------------------------------------------------------
# Integration test: the actual merge-time check, against every tracked file on disk.
# ---------------------------------------------------------------------------


def test_no_tracked_file_contains_a_private_range_ip_literal() -> None:
    result = subprocess.run(
        ["git", "ls-files"],  # noqa: S607 - `git` from PATH, as every suite does
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    tracked_paths = [line for line in result.stdout.splitlines() if line]
    assert tracked_paths, "git ls-files returned nothing -- the check would pass vacuously"

    violations: list[str] = []
    for rel_path in tracked_paths:
        if rel_path == _SELF_PATH:
            continue  # see _SELF_PATH's comment: this file's own fixtures are inert test data
        path = _REPO_ROOT / rel_path
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable (e.g. a broken symlink) -- not a text-literal risk
        for lineno, literal in _find_private_ip_hits(text):
            key = f"{rel_path}:{lineno}"
            if key in _ALLOWLIST:
                continue
            violations.append(f"{key}: {literal}")

    assert violations == [], (
        "found RFC 1918 (private-range) IP literal(s) in tracked file(s) -- this repo is public; "
        "replace with an RFC 5737 documentation-range address (192.0.2.0/24, 198.51.100.0/24, "
        "203.0.113.0/24), or add a justified entry to _ALLOWLIST if it genuinely cannot be "
        "replaced. See D149 in docs/INTEGRATION_HONESTY.md.\n" + "\n".join(violations)
    )
