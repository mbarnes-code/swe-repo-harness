"""SPEC §12.29 at literal scale (round VI task 30) — "Contracts are extracted once,
deterministically, and the graph stays whole."

`tests/test_workers_contracts.py` already proves every *mechanism* §12.29 depends on: the
ownership ladder, the collision detector, and the `extraction_confidence` formula. What it never
built is the criterion's own literal fixture — "three fixture repos vendoring a byte-identical
`identity.proto` plus two carrying only its generated `*_pb2.py`" (five repos) — driven through
the real `fleet scan && fleet sequence` CLI, at the scale the sentence actually names. This file
closes that scale gap, adds the missing node-integrity query (clause (i), for which nothing in
the suite existed), and proves the `divergent` ×0.5 modifier / collision severity-flip as a
worker-level mechanism (see the module docstring on `test_a_divergent_fourth_copy...` for why that
one test is NOT driven through the CLI, and what remains disclosed as unreachable that way).

What each test is for:

* `test_the_literal_five_repo_fixture_meets_every_numeric_and_severity_clause` — the CLI-driven
  scale proof: one `contracts` row, the right `contract_id`, an owner chosen from the three
  real-source repos and never from the two generated-only ones, >=2 consumers, one `CONTRACT`
  collision at `severity='warn'`, and `extraction_confidence` reconstructible from
  `confidence_factors` to within 1e-9 — all read back from the real `contracts`/`collisions`
  tables after a real `fleet scan` and `fleet sequence`.
* `test_a_divergent_fourth_copy_flips_the_collision_and_applies_the_modifier` — the `divergent`
  clause. Real `fleet scan` never populates `ContractsInput.blob_shas` (confirmed against
  `cli.py::_extract_contracts`, which constructs `ContractsInput` with no `blob_shas=` argument at
  all), so this clause cannot be exercised end to end through the CLI today — that gap is the
  disclosed Rule 14 carve-out this task's report names. What CAN be proven, and is proven here, is
  the mechanism itself: `ContractsInput.blob_shas` is a real, documented input field the worker
  already honours (see its own module docstring), so feeding it directly through `_payload`
  (`tests/test_workers_contracts.py`) at the worker level is a legitimate proof that the
  severity-flip and the ×0.5 modifier are real and correct, with the WITHOUT-the-4th-copy fixture
  as its own control (Rule 12): same code path, same blob_sha population mechanism, divergence is
  the only variable.
* `test_the_node_integrity_query_is_silent_on_a_real_scan_and_catches_an_injected_orphan` — clause
  (i). No FK enforces `edges.src_id`/`dst_id` or `wave_members.node_id` against `repos`/
  `contracts` (SQLite has no conditional FK), so `state/schema.sql`'s own comment says this is
  "asserted by ... a test failure rather than a silent orphan" — this is that test. Validated per
  Rule 12: run clean first (real scan, zero rows), THEN inject a deliberately orphaned row of each
  `*_kind` combination the query has to check (`edges.src` REPO, `edges.dst` CONTRACT,
  `wave_members.node` REPO, `wave_members.node` CONTRACT) and confirm the query catches every one.

**Correction, 2026-09-14 (round VIII, §15.1 item 3, Wave 7.3 batch 35) — the paragraph above and
the second bullet's claim ("Real `fleet scan` never populates `ContractsInput.blob_shas`
… confirmed against `cli.py::_extract_contracts`, which constructs `ContractsInput` with no
`blob_shas=` argument at all") were true when this file was written (`843e70c`,
2026-09-03 15:35 UTC) and are FALSE as of this correction: `c9340af` ("D114 (a)+(b): file_blobs
capture mechanism, wire §12.9 criterion (d)"), landed the SAME DAY at 22:10 UTC — under seven
hours later, on a sibling lane this file's author could not have seen — wired `_capture_file_blobs`
(real `git ls-tree` blob SHAs, captured once per repo at scan time) into `_extract_contracts`'s
`blob_shas=` argument, which now reads
`blob_shas={f"{repo}\\x00{path}": sha for (repo, path), sha in blob_shas.items()}` (`cli.py`
current HEAD), fed from `_scan_impl`'s own `file_blobs = await _capture_file_blobs(...)` call
immediately before `_extract_contracts` (`cli.py`, same file). Re-confirmed by reading both sites
directly before writing this correction, not inherited from the stale claim above — this is
exactly the "re-measure a routed finding at the moment you act on it" case CLAUDE.md's Rule 12
warns a finding can rot between filing and fix. **The gap this module docstring's own §3 note
described (no blob SHA ⇒ `content_sha256` empty ⇒ `divergent` can never fire) is CLOSED for the
CLI path** — `test_a_real_fleet_scan_populates_blob_shas_and_flips_the_divergent_collision_end_
to_end` below is the new CLI-driven proof this closure enables, kept alongside (not instead of)
the worker-level test 2 below, which remains a legitimate, narrower proof of the mechanism itself.
Kept here as the record of what was true when this paragraph was written, per this project's
"annotate, never rewrite" convention (CLAUDE.md §7) — not deleted or edited in place.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from fleet.cli import ExitCode, app
from fleet.util.hashing import sha256_text
from fleet.workers.contracts import MODIFIERS
from tests.test_cli import MODELS_YAML
from tests.test_scan_e2e import _fresh_db, _make_repo
from tests.test_workers_contracts import (
    IDENTITY_PROTO,
    IDENTITY_SOURCE,
    PROTO_ID,
    _by_id,
    _payload,
    _pkg,
    _run,
)

runner = CliRunner()

# =======================================================================================
# fixture content — the literal SPEC scale: 3 repos carrying the real `.proto`, 2 carrying
# only generated `*_pb2.py` (SPEC §12 item 29's own words), reusing IDENTITY_PROTO/_pkg from
# tests/test_workers_contracts.py rather than reinventing them.
# =======================================================================================


def _pb2(package: str) -> str:
    """A literal generated-protobuf Python stub — `**/*_pb2.py`, ScanSection's default
    `generated_globs` entry, and the exact suffix SPEC §12 item 29 names. Detection does not
    depend on this content (the glob match alone makes `_is_generated` true), but the content is
    written to look like what `protoc --python_out` actually emits, because a fixture that claims
    to be a generated binding should read like one.
    """
    dotted = package.replace(".", "/")
    return (
        "# -*- coding: utf-8 -*-\n"
        "# Generated by the protocol buffer compiler.  DO NOT EDIT!\n"
        f"# source: {dotted}/identity.proto\n"
        "from google.protobuf import descriptor as _descriptor\n"
        "from google.protobuf import descriptor_pool as _descriptor_pool\n"
        "from google.protobuf import symbol_database as _symbol_database\n"
    )


IDENTITY_PROTO_DIVERGENT = """\
syntax = "proto3";

package acme.identity.v1;

service IdentityService {
  rpc GetUser (GetUserRequest) returns (User);
}

message User {
  string id = 1;
  string display_name = 2;
}

message GetUserRequest {
  string id = 1;
}
"""
"""A real content difference from `IDENTITY_PROTO` (an added `display_name` field) at the SAME
package, so it is still identified as the same `contract_id` — a divergent COPY, not a different
contract."""

# Three real-source carriers. Two are non-vendored (a genuine "two teams independently editing
# the interface" shape `graph/collisions.py::_contract_collisions` requires for its escalation:
# only NON-vendored claims count toward the >=2 needed to flip severity to 'error') and one is
# vendored under `third_party/`, matching `vendor_globs`'s default.
CHECKOUT_SOURCE = "src/api/acme/identity/v1/identity.proto"
BILLING_VENDORED = "third_party/acme/identity/v1/identity.proto"
GENERATED_PB2 = "gen/acme/identity/v1/identity_pb2.py"

SCALE_FLEET: dict[str, dict[str, str]] = {
    "acme-identity": {
        IDENTITY_SOURCE: IDENTITY_PROTO,
        "package.json": _pkg("@acme/identity"),
    },
    "acme-checkout": {
        CHECKOUT_SOURCE: IDENTITY_PROTO,
        "package.json": _pkg("@acme/checkout"),
    },
    "acme-billing": {
        BILLING_VENDORED: IDENTITY_PROTO,
        "package.json": _pkg("@acme/billing"),
    },
    "acme-reporting": {
        GENERATED_PB2: _pb2("acme.identity.v1"),
        "package.json": _pkg("@acme/reporting"),
    },
    "acme-analytics": {
        GENERATED_PB2: _pb2("acme.identity.v1"),
        "package.json": _pkg("@acme/analytics"),
    },
}
"""The literal fixture: `docs/SPEC.md` §12 item 29's "three fixture repos vendoring a
byte-identical `identity.proto` plus two carrying only its generated `*_pb2.py`" — five repos,
not the three `SHARED_FLEET` uses elsewhere in this suite."""

REAL_SOURCE_REPOS = frozenset({"acme-identity", "acme-checkout", "acme-billing"})
GENERATED_ONLY_REPOS = frozenset({"acme-reporting", "acme-analytics"})

DIVERGENT_SOURCE = "src/api/acme/identity/v1/identity.proto"
DIVERGENT_FLEET: dict[str, dict[str, str]] = {
    **SCALE_FLEET,
    "acme-shipping": {
        DIVERGENT_SOURCE: IDENTITY_PROTO_DIVERGENT,
        "package.json": _pkg("@acme/shipping"),
    },
}
"""SCALE_FLEET plus SPEC's "divergent fourth copy" — a fourth REAL (non-vendored) carrier of
`acme.identity.v1` whose content differs. Non-vendored is load-bearing: `_contract_collisions`
only escalates severity when >=2 NON-vendored claims disagree on content."""


def _blob_shas(repos: dict[str, dict[str, str]]) -> dict[str, str]:
    """Deterministic per-file content hashes, keyed exactly as `_blob_sha` expects
    (`'{repo_id}\\x00{path}'`).

    NOT a real git blob SHA — real `fleet scan` captures none today (see
    `src/fleet/workers/contracts.py`'s module docstring) — but a legitimate TEST-ONLY value fed
    into the field that docstring says exists for exactly this: "`ContractsInput.blob_shas` exists
    to be fed the listing the moment preflight captures one; the discovery code below already
    honours it." Content-hashing here (rather than inventing an arbitrary string) means two
    byte-identical files always agree and a real content difference always disagrees, which is the
    only property the worker's `distinct_shas` logic reads.
    """
    return {
        f"{repo_id}\x00{path}": sha256_text(text)
        for repo_id, files in repos.items()
        for path, text in files.items()
    }


# =======================================================================================
# CLI-driving helpers (mirrors tests/test_sequence_e2e.py's own conventions for this fleet)
# =======================================================================================

FLEET_YAML = """\
run:
  monorepo_path: ../acme-monorepo
  cache_dir: cache/
  work_dir: work/
concurrency:
  cpu_pool_workers: 1
  docker: 1
verify:
  container_memory: 64m
budgets:
  max_rss_mb: 512
preflight:
  min_free_bytes: 1048576
  # §12.11/D116 Leg C (round VI task 111, fix round): this fixture's `package.json`s were never
  # vetted to succeed under a REAL native build (no `"test"` script, some declare cross-repo
  # dependency names unpublished by design). Leg C's own red-path gate turns that pre-existing
  # native-baseline failure into a real `phases.status = 'SKIPPED'`, corrupting this file's own
  # contract/hoist assertions, which have nothing to do with baseline behavior. Disabled here
  # entirely for the same reason `tests/test_scan_e2e.py`'s own `FLEET_YAML` disables it.
  baseline_build:
    enabled: false
"""
"""No `graph:` section: contract extraction and hoisting are both on by default
(`ContractsSection.enabled`, `GraphSection.hoist_contracts`), which is the configuration under
test. `preflight.min_free_bytes` is lowered for the same reason `test_sequence_e2e.py`'s copy is —
this fixture is a few kilobytes and the real §11.3 floor would refuse it before Phase 1 starts."""


@pytest.fixture
def scale_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Five real git repos — the literal SCALE_FLEET — a config bundle, and a fresh db."""
    sources = {
        name: _make_repo(tmp_path / "sources", name, dict(files))
        for name, files in SCALE_FLEET.items()
    }
    workspace = tmp_path / "workspace"
    config = workspace / "config"
    config.mkdir(parents=True)
    (config / "fleet.yaml").write_text(FLEET_YAML, encoding="utf-8")
    (config / "models.yaml").write_text(MODELS_YAML, encoding="utf-8")
    entries = "".join(f"  - name: {name}\n    url: {path}\n" for name, path in sources.items())
    (config / "repos.yaml").write_text(
        f"version: 1\ndefaults:\n  ref: main\nrepos:\n{entries}", encoding="utf-8"
    )
    _fresh_db(workspace / "state" / "fleet.db")
    monkeypatch.chdir(workspace)
    return workspace


def _base(root: Path) -> list[str]:
    return ["--config", str(root / "config" / "fleet.yaml"), "--db", str(root / "state/fleet.db")]


def _scan(root: Path, *extra: str) -> Any:
    return runner.invoke(
        app, [*_base(root), "scan", "--skip-classify", *extra], catch_exceptions=False
    )


def _sequence(root: Path, *extra: str) -> Any:
    return runner.invoke(app, [*_base(root), "--json", "sequence", *extra], catch_exceptions=False)


def _query(root: Path, sql: str, params: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(root / "state" / "fleet.db")
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _scan_then_sequence(root: Path) -> None:
    assert _scan(root).exit_code == ExitCode.SUCCESS
    result = _sequence(root)
    assert result.exit_code == ExitCode.SUCCESS, result.output


# =======================================================================================
# 1 — the literal fixture, driven through the real CLI
# =======================================================================================


def test_the_literal_five_repo_fixture_meets_every_numeric_and_severity_clause(
    scale_workspace: Path,
) -> None:
    """The whole non-divergent half of SPEC §12 item 29's sentence, read back from the real
    `contracts`/`collisions` tables after a real `fleet scan && fleet sequence`."""
    _scan_then_sequence(scale_workspace)

    rows = _query(
        scale_workspace,
        "SELECT contract_id, owning_repo_id, consumer_repo_ids, extraction_confidence, "
        "confidence_factors FROM contracts",
    )
    assert len(rows) == 1, f"exactly one contracts row for the whole fleet, got {rows}"
    contract_id, owner, consumers_json, confidence, factors_json = rows[0]
    assert contract_id == PROTO_ID, "the exact contract_id the real worker produces"

    assert owner in REAL_SOURCE_REPOS, owner
    assert owner not in GENERATED_ONLY_REPOS, (
        "zero repos may be proposed as owner on the strength of generated code alone "
        f"(owner={owner!r})"
    )

    consumers = json.loads(consumers_json)
    assert len(consumers) >= 2, consumers

    factors = json.loads(factors_json)
    product = 0.9
    for value in factors.values():
        product *= value
    assert confidence == pytest.approx(product, abs=1e-9), (factors, confidence)

    collisions = _query(
        scale_workspace, "SELECT kind, key, severity FROM collisions WHERE kind = 'CONTRACT'"
    )
    assert collisions == [("CONTRACT", PROTO_ID, "warn")], collisions


# =======================================================================================
# 2 — the divergent fourth copy (worker-level; see module docstring for why not CLI-driven)
# =======================================================================================


def test_a_divergent_fourth_copy_flips_the_collision_and_applies_the_modifier(
    tmp_path: Path,
) -> None:
    """SPEC's `divergent` clause, proven at the worker level with real content hashes.

    Control first (Rule 12): the SAME SCALE_FLEET, run through the SAME blob_shas-populated
    code path but with no divergent copy, must NOT escalate — isolating the escalation to the
    one variable (content divergence) rather than to the mere presence of blob_shas.
    """
    control = _run(_payload(tmp_path / "control", SCALE_FLEET, blob_shas=_blob_shas(SCALE_FLEET)))
    control_collisions = [c for c in control.collisions if c.kind == "CONTRACT"]
    assert len(control_collisions) == 1, control_collisions
    assert control_collisions[0].severity == "warn", (
        "control: identical content everywhere, blob_shas populated, must NOT escalate"
    )
    assert "divergent" not in _by_id(control)[PROTO_ID].confidence_factors

    divergent = _run(
        _payload(tmp_path / "divergent", DIVERGENT_FLEET, blob_shas=_blob_shas(DIVERGENT_FLEET))
    )
    divergent_collisions = [c for c in divergent.collisions if c.kind == "CONTRACT"]
    assert len(divergent_collisions) == 1, divergent_collisions
    assert divergent_collisions[0].severity == "error", (
        "a real, non-vendored content divergence between >=2 claims must escalate"
    )

    node = _by_id(divergent)[PROTO_ID]
    assert node.confidence_factors["divergent"] == MODIFIERS["divergent"] == 0.5
    product = 0.9
    for value in node.confidence_factors.values():
        product *= value
    assert node.extraction_confidence == pytest.approx(product, abs=1e-9)


# =======================================================================================
# 2b — the SAME divergent fourth copy, now driven through a REAL `fleet scan` — the CLI-level
# proof the module docstring's original §3 note (see the dated correction above) said was
# unreachable, closed by D114(a) (`c9340af`) since this file was first written
# =======================================================================================


def test_a_real_fleet_scan_populates_blob_shas_and_flips_the_divergent_collision_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disclosed `_extract_contracts` gap this batch was dispatched to close (round VIII,
    §15.1 item 3, Wave 7.3 batch 35; `research-wave7-scoping-report.md` §3) — re-measured, not
    re-derived: reading `cli.py::_extract_contracts` (current HEAD) shows it already constructs
    `ContractsInput(blob_shas=...)` from a real `blob_shas` argument, and `_scan_impl` already
    feeds it `_capture_file_blobs`'s real `git ls-tree` output (see the module docstring's dated
    correction above). What is actually missing — and what this test supplies — is a test
    proving that wiring end to end: no test anywhere drove `DIVERGENT_FLEET` through a real
    `fleet scan` before this one, only through `_payload`/`_run` directly (test above).

    Uses REAL git blob SHAs (via `_make_repo` + `_capture_file_blobs`'s own `ls-tree`), not the
    synthetic `sha256_text` values `_blob_shas()` (above) feeds the worker-level test — the two
    are deliberately different capture mechanisms, and this test is the one that proves the git
    one, not a restatement of the sha256 one.
    """
    sources = {
        name: _make_repo(tmp_path / "sources", name, dict(files))
        for name, files in DIVERGENT_FLEET.items()
    }
    workspace = tmp_path / "workspace"
    config = workspace / "config"
    config.mkdir(parents=True)
    (config / "fleet.yaml").write_text(FLEET_YAML, encoding="utf-8")
    (config / "models.yaml").write_text(MODELS_YAML, encoding="utf-8")
    entries = "".join(f"  - name: {name}\n    url: {path}\n" for name, path in sources.items())
    (config / "repos.yaml").write_text(
        f"version: 1\ndefaults:\n  ref: main\nrepos:\n{entries}", encoding="utf-8"
    )
    _fresh_db(workspace / "state" / "fleet.db")
    monkeypatch.chdir(workspace)

    _scan_then_sequence(workspace)

    collisions = _query(
        workspace, "SELECT kind, key, severity FROM collisions WHERE kind = 'CONTRACT'"
    )
    assert collisions == [("CONTRACT", PROTO_ID, "error")], (
        "a real git-blob-SHA-backed content divergence must flip the REAL CLI's own collision "
        f"row to 'error', exactly as the worker-level proof shows in isolation: {collisions}"
    )

    factors_json = _query(
        workspace, "SELECT confidence_factors FROM contracts WHERE contract_id = ?", (PROTO_ID,)
    )[0][0]
    factors = json.loads(factors_json)
    assert factors.get("divergent") == MODIFIERS["divergent"] == 0.5, factors


# =======================================================================================
# 3 — node integrity (clause (i)): no FK enforces this, so a query must
# =======================================================================================

NODE_INTEGRITY_QUERY = """\
WITH edge_src_orphans AS (
    SELECT 'edges.src' AS location, e.run_id, e.edge_id AS ref_id,
           e.src_kind AS node_kind, e.src_id AS node_id
    FROM edges e
    WHERE e.run_id = ?
      AND (
        (e.src_kind = 'REPO'
           AND NOT EXISTS (SELECT 1 FROM repos r WHERE r.repo_id = e.src_id))
        OR (e.src_kind = 'CONTRACT'
           AND NOT EXISTS (
               SELECT 1 FROM contracts c
               WHERE c.run_id = e.run_id AND c.contract_id = e.src_id
           ))
      )
),
edge_dst_orphans AS (
    SELECT 'edges.dst' AS location, e.run_id, e.edge_id AS ref_id,
           e.dst_kind AS node_kind, e.dst_id AS node_id
    FROM edges e
    WHERE e.run_id = ?
      AND e.dst_id IS NOT NULL
      AND (
        (e.dst_kind = 'REPO'
           AND NOT EXISTS (SELECT 1 FROM repos r WHERE r.repo_id = e.dst_id))
        OR (e.dst_kind = 'CONTRACT'
           AND NOT EXISTS (
               SELECT 1 FROM contracts c
               WHERE c.run_id = e.run_id AND c.contract_id = e.dst_id
           ))
      )
),
wave_member_orphans AS (
    SELECT 'wave_members.node' AS location, wm.run_id, NULL AS ref_id,
           wm.node_kind AS node_kind, wm.node_id AS node_id
    FROM wave_members wm
    WHERE wm.run_id = ?
      AND (
        (wm.node_kind = 'REPO'
           AND NOT EXISTS (SELECT 1 FROM repos r WHERE r.repo_id = wm.node_id))
        OR (wm.node_kind = 'CONTRACT'
           AND NOT EXISTS (
               SELECT 1 FROM contracts c
               WHERE c.run_id = wm.run_id AND c.contract_id = wm.node_id
           ))
      )
)
SELECT * FROM edge_src_orphans
UNION ALL SELECT * FROM edge_dst_orphans
UNION ALL SELECT * FROM wave_member_orphans;
"""
"""SPEC §12 item 29 clause (i), the query it names but no code shipped:
"every `edges.src_id`/`dst_id` and every `wave_members.node_id` resolves in `repos` or
`contracts` according to its sibling `*_kind` ... asserted by a query returning zero rows, since
no FK enforces it." `state/schema.sql`'s own comment on `edges` says the same thing ("§12.29 makes
a dangling node id a test failure rather than a silent orphan") — this is that test, and this is
that query. `dst_id IS NOT NULL` is a legitimate skip, not a loophole: the schema's own CHECK
(`dst_kind = 'REPO' OR dst_id IS NOT NULL`) makes a NULL `dst_id` mean "external dependency,
outside the fleet" only when `dst_kind = 'REPO'` — there is nothing to resolve.
"""


def _orphans(root: Path, run_id: str) -> list[tuple[Any, ...]]:
    return _query(root, NODE_INTEGRITY_QUERY, (run_id, run_id, run_id))


def test_the_node_integrity_query_is_silent_on_a_real_scan_and_catches_an_injected_orphan(
    scale_workspace: Path,
) -> None:
    """Rule 12: validate the instrument before trusting its clean result.

    First a real scan (this file's own SCALE_FLEET fixture from test 1) must return zero orphan
    rows — nothing here should ever dangle. Only THEN is a deliberately orphaned row of every
    `*_kind` combination the query has to check injected directly into the database, proving the
    query is not silent by construction (an unreachable branch, an always-false predicate) but
    genuinely fires on the known-bad state it exists to catch.
    """
    _scan_then_sequence(scale_workspace)

    (run_id,) = _query(scale_workspace, "SELECT run_id FROM runs")[0]
    assert _orphans(scale_workspace, run_id) == [], "a real scan must resolve every node id"

    (owner_repo_id,) = _query(scale_workspace, "SELECT owning_repo_id FROM contracts LIMIT 1")[0]
    (wave_index,) = _query(
        scale_workspace, "SELECT MIN(wave_index) FROM waves WHERE run_id = ?", (run_id,)
    )[0]

    conn = sqlite3.connect(scale_workspace / "state" / "fleet.db")
    try:
        now = "2026-01-01T00:00:00Z"
        # (a) edges.src, REPO kind, dangling — dst is real, isolating the orphan to src.
        conn.execute(
            "INSERT INTO edges (edge_key, run_id, src_kind, src_id, dst_kind, dst_id, "
            "dst_coord_key, kind, base_confidence, confidence, evidence_path, detected_at) "
            "VALUES (?, ?, 'REPO', 'ghost-repo-src', 'REPO', ?, 'test:orphan-src', "
            "'DECLARED_DEP', 1.0, 1.0, 'orphan-src-test', ?)",
            (sha256_text("orphan-edge-src"), run_id, owner_repo_id, now),
        )
        # (b) edges.dst, CONTRACT kind, dangling — src is real, isolating the orphan to dst.
        conn.execute(
            "INSERT INTO edges (edge_key, run_id, src_kind, src_id, dst_kind, dst_id, "
            "dst_coord_key, kind, base_confidence, confidence, evidence_path, detected_at) "
            "VALUES (?, ?, 'REPO', ?, 'CONTRACT', 'proto:does.not.exist', "
            "'proto:does.not.exist', 'CONTRACT_CONSUME', 1.0, 1.0, 'orphan-dst-test', ?)",
            (sha256_text("orphan-edge-dst"), run_id, owner_repo_id, now),
        )
        # (c) wave_members.node, REPO kind, dangling.
        conn.execute(
            "INSERT INTO wave_members (run_id, wave_index, node_kind, node_id) "
            "VALUES (?, ?, 'REPO', 'ghost-repo-wave')",
            (run_id, wave_index),
        )
        # (d) wave_members.node, CONTRACT kind, dangling.
        conn.execute(
            "INSERT INTO wave_members (run_id, wave_index, node_kind, node_id) "
            "VALUES (?, ?, 'CONTRACT', 'proto:also.does.not.exist')",
            (run_id, wave_index),
        )
        conn.commit()
    finally:
        conn.close()

    orphans = _orphans(scale_workspace, run_id)
    assert len(orphans) == 4, orphans
    locations = {row[0] for row in orphans}
    assert locations == {"edges.src", "edges.dst", "wave_members.node"}, orphans
    node_ids = {row[4] for row in orphans}
    assert node_ids == {
        "ghost-repo-src",
        "proto:does.not.exist",
        "ghost-repo-wave",
        "proto:also.does.not.exist",
    }, orphans
