"""`run_digest`: the run-equivalence proof (SPEC §11.6).

Two runs are equivalent **iff** their digests match, so this hash has one hard requirement above
all others: it must be a pure function of the run's outcome-determining inputs, identical in
every process, on every host, forever. Concretely, and non-negotiably:

* **no `hash()`** — `PYTHONHASHSEED` randomizes it per process, so a digest built on it differs
  between two byte-identical runs and the proof it backs becomes meaningless;
* **no dict/set iteration order** — every section is a `sorted()` list of tuples, and the
  canonical JSON is emitted with `sort_keys=True`;
* **no timestamps, no ids that are reassigned on rebuild** (`edges.edge_id` is a rowid, §6) and
  nothing else that changes between two runs that decided the same things.

Sections, exactly as §11.6 enumerates them: the wave assignment; the ordering edge set; each
`CycleFinding`'s `break_strategy` + `hoisted_contract_ids` + `broken_edge_keys`; every
`(contract_id, status, hoist_target_path)`; every collision resolution; every `Fleet-Patch-Id`
trailer per repo; and every non-empty `(task_id, attempt, tier, context_policy,
approach_signature)` from `attempts` — so a ladder that anchors differently between two runs is
a digest difference rather than an invisible one.

The patch trailers are **read from Git, not from SQLite** (ADR-0024): the digest attests to what
the branches actually contain. This module therefore does not shell out to git — it accepts the
already-read trailers as an argument, and the git walk belongs to its caller.

`RunDigest.sections` carries a per-section digest alongside the whole, because §11.6 requires a
mismatch to *name* the differing component instead of merely reporting disagreement.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final
from uuid import UUID

import aiosqlite

from fleet.util.hashing import sha256_text

__all__ = [
    "DIGEST_SECTIONS",
    "JsonValue",
    "RunDigest",
    "canonical_json",
    "digest_sections",
    "run_digest",
]

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None

#: The section order is fixed and part of the contract; a reordering would change every digest.
DIGEST_SECTIONS: Final[tuple[str, ...]] = (
    "waves",
    "edges",
    "cycles",
    "contracts",
    "collisions",
    "patch_trailers",
    "attempts",
)


@dataclass(frozen=True, slots=True)
class RunDigest:
    """The run digest and the per-section digests that name *which* component differs."""

    digest: str
    sections: dict[str, str]


def canonical_json(value: JsonValue) -> str:
    """The one serialization every digest is taken over.

    `sort_keys` removes dict-order dependence, `ensure_ascii` makes the bytes locale- and
    encoding-independent, `allow_nan=False` refuses the one float family JSON cannot round-trip,
    and the compact separators keep the form stable against `json` default changes.
    """
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    )


def _sha256(text: str) -> str:
    return sha256_text(text)


def digest_sections(sections: Mapping[str, JsonValue]) -> RunDigest:
    """Hash each section, then hash the whole. Pure, synchronous, and process-independent."""
    per_section = {name: _sha256(canonical_json(sections[name])) for name in DIGEST_SECTIONS}
    whole: JsonValue = {name: sections[name] for name in DIGEST_SECTIONS}
    return RunDigest(digest=_sha256(canonical_json(whole)), sections=per_section)


# --------------------------------------------------------------------------------------
# the SQLite-derived sections
# --------------------------------------------------------------------------------------

_SQL_WAVES: Final = """
SELECT wave_index, node_kind, node_id FROM wave_members WHERE run_id = ?
"""

#: The ORDERING edge set: a suppressed feedback edge is deliberately not part of the ordering,
#: and the cycle section already records that it was broken (§3.1 6d).
_SQL_EDGES: Final = """
SELECT src_kind, src_id, dst_kind, dst_id, kind
  FROM edges WHERE run_id = ? AND ordering_suppressed = 0
"""

_SQL_CYCLES: Final = """
SELECT payload FROM findings WHERE run_id = ? AND kind = 'CycleDetected'
"""

_SQL_CONTRACTS: Final = """
SELECT contract_id, status, hoist_target_path FROM contracts WHERE run_id = ?
"""

_SQL_COLLISIONS: Final = """
SELECT kind, key, repo_ids, severity, resolution FROM collisions WHERE run_id = ?
"""

_SQL_ATTEMPTS: Final = """
SELECT task_id, attempt, tier, context_policy, approach_signature
  FROM attempts WHERE run_id = ? AND approach_signature <> ''
"""


async def _fetch(
    conn: aiosqlite.Connection, sql: str, run_id: str
) -> list[tuple[object, ...]]:
    async with conn.execute(sql, (run_id,)) as cursor:
        return [tuple(row) for row in await cursor.fetchall()]


def _text(value: object) -> str:
    """`None` and `''` are the same absence for digest purposes, and both sort identically."""
    return "" if value is None else str(value)


def _row(*cells: str | int) -> JsonValue:
    """One digest row. Built as `list[JsonValue]` so the recursive alias accepts it."""
    out: list[JsonValue] = list(cells)
    return out


def _patch_row(repo_id: str, trailers: Sequence[str]) -> JsonValue:
    inner: list[JsonValue] = [str(trailer) for trailer in trailers]
    out: list[JsonValue] = [repo_id, inner]
    return out


async def run_digest(
    conn: aiosqlite.Connection,
    run_id: UUID,
    *,
    patch_trailers: Mapping[str, Sequence[str]] | None = None,
) -> RunDigest:
    """Compute the §11.6 run digest for `run_id`.

    `patch_trailers` maps `repo_id` → the `Fleet-Patch-Id` trailers read off `migrate/<repo>` **in
    commit order** — read from Git by the caller (ADR-0024). Repos are sorted; the trailers
    within a repo are NOT, because their order is the commit order and reordering two patches is
    a real difference between runs.

    Read-only: this issues `SELECT`s on the caller's handle and opens no transaction of its own,
    so it is safe on a `mode=ro` connection and never contends for the write lock.
    """
    rid = str(run_id)
    trailers = dict(patch_trailers or {})

    waves = sorted(
        (_text(k), _text(n), _text(i)) for k, n, i in await _fetch(conn, _SQL_WAVES, rid)
    )
    edges = sorted(
        (_text(sk), _text(si), _text(dk), _text(di), _text(kind))
        for sk, si, dk, di, kind in await _fetch(conn, _SQL_EDGES, rid)
    )
    cycles = sorted(
        (_cycle_section(str(payload)) for (payload,) in await _fetch(conn, _SQL_CYCLES, rid)),
        key=canonical_json,
    )
    contracts = sorted(
        (_text(cid), _text(status), _text(path))
        for cid, status, path in await _fetch(conn, _SQL_CONTRACTS, rid)
    )
    collisions = sorted(
        (_text(kind), _text(key), _text(repo_ids), _text(severity), _text(resolution))
        for kind, key, repo_ids, severity, resolution in await _fetch(conn, _SQL_COLLISIONS, rid)
    )
    # §11.6 sorts attempts by (task_id, attempt, approach_signature) — `attempt` numerically,
    # which is why it stays an int rather than joining the other rows as text.
    attempts = sorted(
        (
            (_text(task_id), int(str(attempt)), _text(tier), _text(policy), _text(signature))
            for task_id, attempt, tier, policy, signature in await _fetch(
                conn, _SQL_ATTEMPTS, rid
            )
        ),
        key=lambda row: (row[0], row[1], row[4]),
    )

    sections: dict[str, JsonValue] = {
        "waves": [_row(*row) for row in waves],
        "edges": [_row(*row) for row in edges],
        "cycles": list(cycles),
        "contracts": [_row(*row) for row in contracts],
        "collisions": [_row(*row) for row in collisions],
        "patch_trailers": [_patch_row(repo, trailers[repo]) for repo in sorted(trailers)],
        "attempts": [_row(*row) for row in attempts],
    }
    return digest_sections(sections)


def _cycle_section(payload: str) -> JsonValue:
    """`break_strategy` + `hoisted_contract_ids` + `broken_edge_keys` off a `CycleFinding` JSON.

    Read straight from the persisted JSON rather than through the model: the digest must not
    change because a model default changed, only because the recorded decision changed.
    """
    finding = json.loads(payload)
    if not isinstance(finding, dict):
        return _row()
    hoisted: list[JsonValue] = [
        *sorted(_text(x) for x in finding.get("hoisted_contract_ids") or ())
    ]
    broken: list[JsonValue] = [*sorted(_text(x) for x in finding.get("broken_edge_keys") or ())]
    section: dict[str, JsonValue] = {
        "scc_id": _text(finding.get("scc_id")),
        "break_strategy": _text(finding.get("break_strategy")),
        "hoisted": hoisted,
        "broken": broken,
    }
    return section
