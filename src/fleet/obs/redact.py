"""The single `redact()` applied at EVERY egress boundary (SPEC §11.4).

One function, called on every log line, every event payload, every `last_error`, every artifact
write and every PR body. Credentials in a clone URL, tokens in tool output, and any value taken
from a secret-bearing environment variable never reach disk.

**Why this is not an opt-in helper.** The threat is a *forgotten* call, not a missing feature.
So `redact()` is wired into the boundaries themselves — `obs.log.configure()` installs
`redaction_processor` in the structlog pipeline and `obs.events.EventEmitter` redacts every
payload before both the JSONL sink and the `events` insert — and a caller that never mentions
redaction is still redacted.

**Placeholder shape (§11.4).** A match becomes `«redacted:{kind}:{fp8}»`, where `fp8` is the
first 8 hex of `sha256(value)`. The same secret is therefore recognisably the same value
everywhere in the log without ever being recoverable from it, and `kind` names *which* detector
fired, so a human debugging the line still knows a GitHub PAT was there.

This is deliberately **not** `github_pat_***REDACTED***`: §12.20 greps `logs/`, `artifacts/` and
`migration_state.json` for the literal string `github_pat_` and requires zero hits, so keeping
the provider prefix would fail the harness's own leak criterion. `kind` carries that information
in a form the grep cannot mistake for a live token (CLAUDE.md Rule 7: the spec's criterion wins,
and the reason is recorded here).

The same criterion is why URL userinfo is replaced *including* its `@`: leaving the `@` in place
would leave `://«redacted:url_userinfo:ab12cd34»@host` matching §12.20's
`://[^/[:space:]:@]+:[^/[:space:]@]+@` — the placeholder's own colons re-create the pattern. The
host survives, which is all a human needs.

**Layered detection.** High-precision provider patterns first, then URL userinfo, then structural
rules (a secret-ish dict key, an `Authorization:` header, a `Bearer` credential), then two
generic rules that are deliberately conservative: a value taken verbatim from a secret-bearing
environment variable, and a ≥20-character token next to a key-ish word whose Shannon entropy is
at least 4.0 bits/char. The entropy gate exists to stop over-redaction: a 64-char SHA-256 digest
(~3.9 bits/char over 16 symbols), a long path and a long branch name stay readable, because a log
that redacts everything is a log nobody can debug with.

Nothing here mutates its argument: `redact()` returns new containers, always.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Final, overload

__all__ = [
    "ENTROPY_MIN_BITS",
    "ENTROPY_MIN_LEN",
    "MAX_DEPTH",
    "PATTERNS",
    "JSONValue",
    "RedactionPattern",
    "fingerprint",
    "placeholder",
    "redact",
    "redact_mapping",
    "redact_text",
    "redaction_counts",
    "reset_redaction_counts",
]

type JSONValue = str | int | float | bool | Sequence[JSONValue] | Mapping[str, JSONValue] | None
"""What may cross an egress boundary: JSON-shaped data, redacted leaf by leaf."""

#: §11.4 `redaction.entropy_min_bits` / `entropy_min_len` — the generic fallback's two gates.
ENTROPY_MIN_BITS: Final = 4.0
ENTROPY_MIN_LEN: Final = 20

#: Deeper than this and the subtree is rendered through `redact_text` instead of walked, so a
#: pathological (or cyclic) payload cannot blow the stack inside a log call.
MAX_DEPTH: Final = 12

_MIN_ENV_SECRET_LEN: Final = 8


@dataclass(frozen=True, slots=True)
class RedactionPattern:
    """One detector. `group`, when set, is the only span replaced — the rest is context kept."""

    kind: str
    regex: re.Pattern[str]
    group: str | None = None
    entropy_gated: bool = False


#: A word that makes an adjacent long token a credential rather than an identifier. Anchored with
#: `\b` on BOTH sides so `token_count` (`_` is a word char) never matches.
_KEYISH: Final = (
    r"(?:api[_-]?keys?|secret[_-]?keys?|access[_-]?keys?|private[_-]?keys?|auth[_-]?tokens?"
    r"|access[_-]?tokens?|refresh[_-]?tokens?|client[_-]?secrets?|passwords?|passwd|credentials?"
    r"|secrets?|tokens?|apikeys?)"
)

#: A dict key whose *last* word is a credential word. Trailing-anchored on purpose: `github_token`
#: and `api_key` are secrets, `token_count` and `secret_scan_findings` are not.
_SECRET_KEY_NAME: Final = re.compile(
    r"(?i)^(?:.*[_.\-])?(?:secrets?|passwords?|passwd|api_?keys?|access_?keys?|private_?keys?"
    r"|credentials?|authorization|auth_?tokens?|access_?tokens?|refresh_?tokens?|client_?secrets?"
    r"|tokens?|pat)$"
)

#: An env var whose *value* is a live credential. Same trailing-word discipline as above.
_SECRET_ENV_NAME: Final = re.compile(
    r"(?i)^(?:.*[_.\-])?(?:secrets?|passwords?|passwd|api_?keys?|access_?keys?|private_?keys?"
    r"|credentials?|auth_?tokens?|access_?tokens?|refresh_?tokens?|client_?secrets?|tokens?|pat)$"
)

#: Env vars whose name reads secret-ish but whose value is a path or a socket, not a credential.
_ENV_NAME_SUFFIX_DENY: Final = ("_PATH", "_FILE", "_DIR", "_SOCK", "_SOCKET", "_ENV", "_NAME")

PATTERNS: Final[tuple[RedactionPattern, ...]] = (
    # -- high-precision provider patterns (§11.4 `redaction.patterns`) --------------------
    RedactionPattern("github_pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    RedactionPattern("github_classic", re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}")),
    RedactionPattern("slack", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    RedactionPattern("aws_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    RedactionPattern("anthropic", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    RedactionPattern("openai_style", re.compile(r"sk-(?!ant-)[A-Za-z0-9]{32,}")),
    RedactionPattern(
        "gcp_sa_key",
        re.compile(r"(?P<secret>\"private_key_id\"\s*:\s*\"[a-f0-9]{40}\")"),
        group="secret",
    ),
    RedactionPattern("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    # -- URL userinfo: `https://oauth2:github_pat_…@host/…`. The `@` goes with it (see docstring).
    RedactionPattern(
        "url_userinfo",
        re.compile(r"(?<=://)(?P<secret>[^/\s:@]+:[^/\s@]+@)"),
        group="secret",
    ),
    # -- structural: headers and bearer credentials ---------------------------------------
    RedactionPattern(
        "authorization",
        re.compile(r"(?i)\bauthorization\b\s*[:=]\s*[\"']?(?P<secret>[^\r\n\"',]{4,})"),
        group="secret",
    ),
    RedactionPattern(
        "bearer",
        re.compile(r"(?i)\bbearer\s+(?P<secret>[A-Za-z0-9._~+/\-]{12,}=*)"),
        group="secret",
    ),
    # -- generic, entropy-gated: a long high-entropy token next to a key-ish word ----------
    RedactionPattern(
        "high_entropy",
        re.compile(
            rf"(?i)\b{_KEYISH}\b[\"']?\s*[:=]\s*[\"']?"
            rf"(?P<secret>[^\s\"',;}}\])]{{{ENTROPY_MIN_LEN},}})"
        ),
        group="secret",
        entropy_gated=True,
    ),
)

_counts: Counter[str] = Counter()


def redaction_counts() -> Mapping[str, int]:
    """`redactions_total{kind}` (§11.4): how many secrets each detector caught, never the values."""
    return dict(_counts)


def reset_redaction_counts() -> None:
    """Test and per-run hook. Counters are diagnostics, not state."""
    _counts.clear()


def fingerprint(value: str) -> str:
    """`sha256(value)[:8]` — stable across the run, one-way, safe to print."""
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:8]


def placeholder(kind: str, value: str) -> str:
    """The §11.4 replacement, `«redacted:{kind}:{fp8}»`, and the counter bump that goes with it."""
    _counts[kind] += 1
    return f"«redacted:{kind}:{fingerprint(value)}»"


def _shannon_bits(value: str) -> float:
    """Shannon entropy in bits per character. A 64-char hex digest scores ~3.9, under the gate."""
    if not value:
        return 0.0
    total = len(value)
    return -sum(
        (n / total) * math.log2(n / total) for n in Counter(value).values()
    )


def _iter_env_secrets() -> list[tuple[str, str]]:
    """`(var_name, value)` for every env var that names a credential, longest value first.

    Read on every call rather than cached: a cache that predates an `os.environ` mutation is a
    secret that silently stops being redacted, and scanning a process environment is microseconds.
    Longest-first so a value that contains another value is replaced whole.
    """
    found: list[tuple[str, str]] = []
    for name, value in os.environ.items():
        if len(value) < _MIN_ENV_SECRET_LEN or value.startswith(("/", "~")):
            continue
        upper = name.upper()
        if upper.endswith(_ENV_NAME_SUFFIX_DENY):
            continue
        if _SECRET_ENV_NAME.match(name):
            found.append((name, value))
    found.sort(key=lambda item: len(item[1]), reverse=True)
    return found


def redact_text(text: str) -> str:
    """Scrub every detector's matches out of one string. Returns a new string."""
    if not text:
        return text

    result = text
    for name, secret in _iter_env_secrets():
        if secret in result:
            result = result.replace(secret, placeholder(f"env.{name}", secret))

    for pattern in PATTERNS:
        result = pattern.regex.sub(partial(_replace, pattern=pattern), result)
    return result


def _replace(match: re.Match[str], *, pattern: RedactionPattern) -> str:
    """Build the replacement for one match, keeping every span outside `group` verbatim."""
    whole = match.group(0)
    secret = whole if pattern.group is None else match.group(pattern.group)
    if pattern.entropy_gated and _shannon_bits(secret) < ENTROPY_MIN_BITS:
        return whole  # a long, low-entropy value: a digest or a path, not a credential
    token = placeholder(pattern.kind, secret)
    if pattern.group is None:
        return token
    start, end = match.span(pattern.group)
    offset = match.start()
    return whole[: start - offset] + token + whole[end - offset :]


@overload
def redact(value: str) -> str: ...
@overload
def redact(value: Mapping[str, JSONValue]) -> dict[str, JSONValue]: ...
@overload
def redact(value: JSONValue) -> JSONValue: ...
def redact(value: JSONValue) -> JSONValue:
    """Redact a string, or recursively every string inside a JSON-shaped structure.

    **Never mutates the argument.** Callers log objects they still own — a redaction that edited
    the caller's dict in place would silently change the value a worker is about to act on, which
    is a far worse bug than a leaked log line.
    """
    return _walk(value, 0)


def redact_mapping(payload: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
    """`redact()` narrowed to the payload shape events and log lines actually carry."""
    return _walk_mapping(payload, 0)


def _walk(value: JSONValue, depth: int) -> JSONValue:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bool) or value is None or isinstance(value, int | float):
        return value
    if depth >= MAX_DEPTH:
        return redact_text(repr(value))
    if isinstance(value, Mapping):
        return _walk_mapping(value, depth)
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [_walk(item, depth + 1) for item in value]
    # Anything else (a datetime, an enum, a model): rendered, then redacted. Rendering an
    # unexpected type is always safe; *dropping* it would lose the log line it was part of.
    return redact_text(str(value))


def _walk_mapping(payload: Mapping[str, JSONValue], depth: int) -> dict[str, JSONValue]:
    out: dict[str, JSONValue] = {}
    for key, item in payload.items():
        name = str(key)
        if isinstance(item, str) and _SECRET_KEY_NAME.match(name):
            # The key alone is the evidence: `{"api_key": "hunter2"}` is a secret at any length.
            out[name] = placeholder("key_name", item)
        else:
            out[name] = _walk(item, depth + 1)
    return out
