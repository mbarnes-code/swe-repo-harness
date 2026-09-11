"""sha256 helpers for manifests, configs and content-addressed LLM cache keys (SPEC §8).

Every hash in the harness is a lowercase hex sha256, because that is the shape the Pydantic
models validate (`pattern=r"^[0-9a-f]{64}$"`).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

_CHUNK = 1 << 20


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str, *, encoding: str = "utf-8") -> str:
    return sha256_bytes(text.encode(encoding))


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def cache_key(*parts: str) -> str:
    """Join key components with a length-prefixed encoding, so no boundary can shift.

    Each part is joined as `str(len(part)) + ":" + part` before hashing. This is unambiguous for
    ANY part content — unlike a bare separator-joined string, which collides whenever a part can
    itself contain the separator (`cache_key("a|b", "c")` == `cache_key("a", "b|c")` under a plain
    `"|".join`). Some callers' parts are free-text config data (e.g. `BackendTarget.model_id`,
    documented as "opaque to the harness"), so that collision is reachable, not merely theoretical.
    """
    return sha256_text("".join(f"{len(part)}:{part}" for part in parts))
