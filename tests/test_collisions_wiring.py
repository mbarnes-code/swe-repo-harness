"""§3.1 step 8's `COORDINATE` detector, from real scanned repositories to a persisted row.

`graph/collisions.py` had five detectors, a `blocking` property and thirteen unit tests, and
exactly one production caller — `workers/contracts.py`, which passes `contracts=` and nothing
else. `fleet sequence` refused unresolved collisions it could not itself detect, and
`graph/sequence.py` mentioned collisions nowhere. So `COORDINATE`, `DEST_PATH` and `FILE_PATH`
were implemented, unit-tested, and unreachable from any CLI path: a fleet where two repos publish
one coordinate migrated silently, and the only end-to-end coverage of exit 6 planted its
`collisions` row by hand with raw SQL, which exercises the gate and none of the audit.

This file covers the `COORDINATE` half of that gap end to end. `DEST_PATH` and `FILE_PATH` remain
unreachable and are deliberately NOT tested here:

* `FILE_PATH` needs a per-file path/blob-SHA listing. Nothing captures one — `workers/clone.py`
  streams `cat-file --batch-check` and keeps only a running maximum — and `state/schema.sql` has
  no table to hold one, as `workers/contracts.py`'s module docstring states outright. Without
  SHAs `len(shas) <= 1` is vacuously true, so the detector's `dedupe` branch would fire for every
  group INCLUDING genuinely divergent files: a "two identical LICENSEs do not fail the run" test
  would pass here for the wrong reason and would pass identically before this file existed.
* `DEST_PATH` can be computed, but `dest_rewrites` has no consumer: nothing in `src/` reads a
  `collisions` resolution back, and Phase 3 places repos with `cli._dest_for`. Persisting the row
  would assert a relocation the harness does not perform.

Both are scheduled as later work. The point of writing it down here is that this file's silence
about them is a measured boundary and not an oversight.

WHAT EACH TEST CATCHES THAT THE PRE-WIRING TREE MISSED — every one of these fails at the parent
commit, and the ladder tests fail for a second, sharper reason than "no row exists":

* `..._is_recorded_as_a_collision_row`      no `COORDINATE` row was ever written by any command.
* `..._by_path_depth_not_by_repo_id`        the winner was `repo_ids[0]`, self-labelled
                                            `ladder:lexicographic-repo-id` while §13 row 6
                                            specifies path depth first. The fixture is built so
                                            the two rules disagree.
* `..._by_commit_count_when_depth_ties`     rung (iii) did not exist either.
* `..._owns_hint_still_outranks_the_ladder` the hint kept working, but a hint naming a repo that
                                            does not publish the coordinate used to win anyway.
* `..._exits_6_with_the_row_already_written` nothing detected this, and the exit-6 path had never
                                            been shown to leave the operator a row to act on.
* `..._writes_no_row_when_nothing_collides` the control: a detector that fires on every fleet is
                                            worse than no detector. Green in BOTH trees, which is
                                            its job — it is not a discriminator and is not
                                            counted as one.

WHICH MUTATION REDDENS WHICH CASE — measured, not asserted. Seven mutations were run against the
wired tree, each read for a non-zero `git diff --numstat` against a backup BEFORE its result was
read, and each interpreter pinned to this worktree:

    ladder -> lexicographic repo_id      depth, commit-count, hint-refused
    drop the detector's publisher guard  hint-refused                        (unique)
    raise before persisting the rows     exits-6-with-row                    (unique)
    hint map -> lowest repo_id           hint-from-publisher                 (unique)
    severity always `error`              is-recorded-as-a-row                (unique)
    drop the commit-count rung           commit-count                        (unique)
    cosmetic reflow (control)            nothing                             (green)

`..._by_path_depth_not_by_repo_id` is the one case that is NOT the unique discriminator of any
mutation here: every ladder mutation that reddens it also reddens `..._naming_no_publisher_...`,
which runs the same fixture with a hint added. It is kept anyway, and the redundancy is written
down rather than hidden: it is the plainest statement of the rule §13 row 6 actually specifies,
and deleting it would leave that rule asserted only as a side effect of a test about `owns:`.

WHAT THIS FILE STILL CANNOT CATCH: the ladder is bound to the detector, not to §13 row 6's prose.
A consistent rewrite of both the SPEC sentence and `ownership_rank` to one wrong ladder passes
everything here. Binding prose to code is `tests/test_floor_rule_statements.py`'s technique and is
not applied to this rule by anything yet.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from fleet.cli import ExitCode, app
from tests.test_cli import MODELS_YAML
from tests.test_scan_e2e import _fresh_db, _git, _make_repo

runner = CliRunner()

FLEET_YAML = """\
run:
  monorepo_path: ../acme-monorepo
  cache_dir: cache/
  work_dir: work/
concurrency:
  cpu_pool_workers: 1
preflight:
  min_free_bytes: 1048576
"""

SHARED = "npm:@acme:shared"
"""`Coordinate.key` for `@acme/shared` — `"{ecosystem}:{group}:{name}"`, ADR-0017."""


def _pkg(name: str) -> str:
    return json.dumps({"name": name, "version": "1.0.0"}, indent=2)


def _workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repos: Mapping[str, Mapping[str, str]],
    *,
    owns: Mapping[str, tuple[str, ...]] = {},
    extra_commits: Mapping[str, int] = {},
) -> Path:
    """A fleet of real git repositories, a config bundle naming them, and a fresh database.

    `extra_commits` exists to drive ladder rung (iii): `repos.commit_count` comes from a real
    `rev-list --count --all` over a real repository, so the only way to give one repo more
    commits than another is to make more commits.
    """
    sources = {
        name: _make_repo(tmp_path / "sources", name, dict(files))
        for name, files in repos.items()
    }
    for name, count in sorted(extra_commits.items()):
        for n in range(count):
            (sources[name] / f"extra{n}.txt").write_text(f"{n}\n", encoding="utf-8")
            _git(sources[name], "add", "-A")
            _git(sources[name], "commit", "-m", f"extra {n}")

    workspace = tmp_path / "workspace"
    config = workspace / "config"
    config.mkdir(parents=True)
    (config / "fleet.yaml").write_text(FLEET_YAML, encoding="utf-8")
    (config / "models.yaml").write_text(MODELS_YAML, encoding="utf-8")
    entries = ""
    for name, path in sources.items():
        entries += f"  - name: {name}\n    url: {path}\n"
        if owns.get(name):
            listed = "".join(f"      - {item}\n" for item in owns[name])
            entries += f"    owns:\n{listed}"
    (config / "repos.yaml").write_text(
        f"version: 1\ndefaults:\n  ref: main\nrepos:\n{entries}", encoding="utf-8"
    )
    _fresh_db(workspace / "state" / "fleet.db")
    monkeypatch.chdir(workspace)
    return workspace


def _base(root: Path) -> list[str]:
    return ["--config", str(root / "config" / "fleet.yaml"), "--db", str(root / "state/fleet.db")]


def _scan(root: Path) -> Any:
    return runner.invoke(
        app, [*_base(root), "scan", "--skip-classify"], catch_exceptions=False
    )


def _sequence(root: Path) -> Any:
    return runner.invoke(app, [*_base(root), "--json", "sequence"], catch_exceptions=False)


def _query(root: Path, sql: str, params: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(root / "state" / "fleet.db")
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _collisions(root: Path, kind: str = "COORDINATE") -> list[tuple[Any, ...]]:
    return _query(
        root,
        "SELECT key, repo_ids, severity, resolution FROM collisions WHERE kind = ? "
        " ORDER BY key",
        (kind,),
    )


def _scan_then_sequence(root: Path) -> None:
    assert _scan(root).exit_code == ExitCode.SUCCESS
    result = _sequence(root)
    assert result.exit_code == ExitCode.SUCCESS, result.output


# =======================================================================================
# the row exists at all
# =======================================================================================

TWO_PUBLISHERS: Mapping[str, Mapping[str, str]] = {
    "acme-alpha": {"package.json": _pkg("@acme/shared"), "src/index.ts": "export const a = 1;\n"},
    "acme-omega": {"package.json": _pkg("@acme/shared"), "src/index.ts": "export const o = 1;\n"},
}
"""Two real repos whose real `package.json`s declare the SAME npm name. The npm adapter parses
both for real; `coordinates.coord_key` is a PRIMARY KEY, so the second insert is dropped by its
`ON CONFLICT` and that table cannot show the contest — `manifests`, keyed `(repo_id, path)`,
keeps both claims, which is what the audit reads."""


def test_two_repos_publishing_one_coordinate_is_recorded_as_a_collision_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wiring itself: real repos → `fleet scan` → `fleet sequence` → a persisted row.

    Before this wiring the whole path produced nothing. `audit_collisions` was reachable only
    from `workers/contracts.py`, which passes `contracts=` and never `coordinates=`, so no
    invocation of any command could write a `COORDINATE` row — the fleet migrated silently with
    two repos claiming one package name.

    `severity='warn'` with a resolution is the correct outcome and is asserted deliberately: two
    repos publishing one coordinate in ONE ecosystem is a fleet fact the ownership ladder settles,
    not a normalization bug. It must be recorded and must NOT block.
    """
    root = _workspace(tmp_path, monkeypatch, TWO_PUBLISHERS)
    _scan_then_sequence(root)

    rows = _collisions(root)
    assert len(rows) == 1, f"exactly one contest in this fleet: {rows}"
    key, repo_ids, severity, resolution = rows[0]
    assert key == SHARED
    assert json.loads(repo_ids) == ["acme-alpha", "acme-omega"]
    assert severity == "warn", "one ecosystem, two publishers: a fleet fact, not a bug"
    assert resolution is not None and resolution.endswith(":acme-alpha")


# =======================================================================================
# the ladder — §13 row 6, rung by rung
# =======================================================================================

DEPTH_DISAGREES: Mapping[str, Mapping[str, str]] = {
    # lexicographically FIRST, but declares the coordinate one directory DOWN
    "acme-alpha": {
        "packages/lib/package.json": _pkg("@acme/shared"),
        "package.json": _pkg("@acme/alpha-root"),
    },
    # lexicographically LAST, but declares it at the ROOT — the shallower path, so the ladder
    # picks it and `repo_ids[0]` does not. The two rules disagree here by construction.
    "acme-omega": {"package.json": _pkg("@acme/shared")},
}


def test_the_owner_is_chosen_by_path_depth_not_by_repo_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§13 row 6's ladder is `owns:` → **shallowest path** → `commit_count` → `repo_id`.

    The pre-wiring detector took `repo_ids[0]` and labelled it `ladder:lexicographic-repo-id`,
    which is rung (iv) presented as the whole ladder. This fixture is built so the two rules
    disagree: `acme-alpha` sorts first and declares `@acme/shared` at `packages/lib/`, while
    `acme-omega` sorts last and declares it at the root. Depth wins, so the answer is
    `acme-omega` — a test that passes under the old rule cannot exist for this fixture.

    The resolution string is asserted too, not only the winner. A self-describing label is what
    lets an operator reading the row know WHICH rung decided, and the old label was a false
    description of a real behaviour.
    """
    root = _workspace(tmp_path, monkeypatch, DEPTH_DISAGREES)
    _scan_then_sequence(root)

    rows = _collisions(root)
    contest = [r for r in rows if r[0] == SHARED]
    assert len(contest) == 1, f"{SHARED} must be contested exactly once: {rows}"
    _, repo_ids, _severity, resolution = contest[0]
    assert json.loads(repo_ids) == ["acme-alpha", "acme-omega"]
    assert resolution == "ladder:depth,commit-count,repo-id:acme-omega", (
        "the SHALLOWEST declaring path owns the coordinate; lexicographic order would have "
        "said acme-alpha"
    )


COMMITS_DISAGREE: Mapping[str, Mapping[str, str]] = {
    # both declare at the root, so rung (ii) ties and rung (iii) has to decide
    "acme-alpha": {"package.json": _pkg("@acme/shared")},
    "acme-omega": {"package.json": _pkg("@acme/shared")},
}


def test_the_owner_is_chosen_by_commit_count_when_path_depth_ties(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rung (iii), isolated: equal depth, and the busier repository wins.

    This is the discriminating case rung (ii) cannot reach. Both manifests sit at the root, so
    depth is equal for both and cannot decide; `acme-omega` is given four extra real commits, so
    `repos.commit_count` decides and again contradicts `repo_ids[0]`. Without this test the depth
    fixture above would leave rung (iii) unexercised — it never ties there.
    """
    root = _workspace(
        tmp_path, monkeypatch, COMMITS_DISAGREE, extra_commits={"acme-omega": 4}
    )
    _scan_then_sequence(root)

    counts = dict(_query(root, "SELECT repo_id, commit_count FROM repos ORDER BY repo_id"))
    assert counts["acme-omega"] > counts["acme-alpha"], (
        f"the fixture must really differ in commit_count or rung (iii) is untested: {counts}"
    )

    (row,) = [r for r in _collisions(root) if r[0] == SHARED]
    assert row[3] == "ladder:depth,commit-count,repo-id:acme-omega"


#: Sorts BEFORE both publishers, which is the whole point of the name: a bystander that sorted
#: last could never shadow a real publisher's hint, and the two tests below would pass whether or
#: not the code that stops it exists.
BYSTANDER = "acme-aaa-bystander"


def test_an_owns_hint_from_a_publisher_outranks_the_ladder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rung (i) beats rungs (ii)–(iv), and a non-publisher cannot shadow it by sorting earlier.

    `acme-alpha` declares `@acme/shared` one directory down, so the ladder alone crowns
    `acme-omega` — the depth test above proves exactly that over this same layout — and the
    `owns:` hint has to override it. What makes this test discriminating rather than decorative
    is `{BYSTANDER}`: it publishes something else entirely, claims `@acme/shared` in its `owns:`,
    and **sorts first**. `owns_hints` maps a key to ONE repo, so a hint map that simply took the
    lowest `repo_id` would hand the detector the bystander and lose the operator's real
    instruction before it was ever read.
    """
    repos = dict(DEPTH_DISAGREES) | {BYSTANDER: {"package.json": _pkg("@acme/bystander")}}
    root = _workspace(
        tmp_path, monkeypatch, repos, owns={BYSTANDER: (SHARED,), "acme-alpha": (SHARED,)}
    )
    _scan_then_sequence(root)

    (row,) = [r for r in _collisions(root) if r[0] == SHARED]
    assert json.loads(row[1]) == ["acme-alpha", "acme-omega"], (
        "a non-publisher is not a party to the contest even when it claims the coordinate"
    )
    assert row[3] == "owns-hint:acme-alpha", (
        "the publisher's hint wins; the earlier-sorting bystander's identical hint must not"
    )


def test_an_owns_hint_naming_no_publisher_is_refused_and_the_ladder_decides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of rung (i): a hint that names a repo publishing nothing is not a
    resolution of this contest.

    Only `{BYSTANDER}` hints `@acme/shared` here, and it publishes `@acme/bystander`. So the
    hint map has no publisher to prefer and passes the bystander through — which is deliberate,
    because it keeps "refuse a hint" as ONE judgement made in the detector. The detector then
    declines it and falls to the ladder, giving `acme-omega` on depth.

    This is the case the `owns:`-hint guard exists for, and the only one that can exercise it:
    where a publisher also hints the coordinate the map never offers a non-publisher at all.
    Crowning the bystander would write an owner no manifest in the fleet backs.
    """
    repos = dict(DEPTH_DISAGREES) | {BYSTANDER: {"package.json": _pkg("@acme/bystander")}}
    root = _workspace(tmp_path, monkeypatch, repos, owns={BYSTANDER: (SHARED,)})
    _scan_then_sequence(root)

    (row,) = [r for r in _collisions(root) if r[0] == SHARED]
    assert row[3] == "ladder:depth,commit-count,repo-id:acme-omega", (
        "an owns: hint naming a non-publisher must not decide a contest it is not party to"
    )


# =======================================================================================
# exit 6, and the row an operator has to act on
# =======================================================================================


def test_a_divergent_ecosystem_contest_exits_6_with_the_row_already_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§10 exit 6 from THIS run's audit, and the row is durable before the process refuses.

    **The fault here is injected, and that is deliberate — it cannot arise from a fixture.**
    `Coordinate.key` is `"{ecosystem}:{group}:{name}"`, so two claims on one `coord_key` always
    agree about the ecosystem token inside that key. The detector's divergence test compares the
    manifest's OWN `ecosystem` column against the others on the same key, and a disagreement
    there means an adapter emitted a `ManifestRef` whose `ecosystem` contradicts the coordinate it
    published — a normalization bug, which is exactly what §3.1 step 8 calls `error`. No correct
    adapter produces one, so the bug is injected into a real, fully scanned database: everything
    downstream of that one column — claim assembly, the detector, the upsert, the gate, the exit
    code — is the real thing.

    Two properties, and the second is the one with no prior coverage anywhere:

    1. `fleet sequence` exits 6. The pre-existing e2e test for this planted a finished
       `collisions` row with raw SQL, so it proved the gate reads the table and nothing about
       whether anything fills it.
    2. **The row is in the database when it exits.** The refusal is raised after
       `StateWriter.submit` has awaited its transaction, never instead of it — an operator told
       to go and resolve a collision must be able to find it. `orchestrator/findings.py`
       documents the same ordering for the exit-8 halt.
    """
    root = _workspace(tmp_path, monkeypatch, TWO_PUBLISHERS)
    assert _scan(root).exit_code == ExitCode.SUCCESS

    conn = sqlite3.connect(root / "state" / "fleet.db")
    try:
        changed = conn.execute(
            "UPDATE manifests SET ecosystem = 'pypi' "
            " WHERE repo_id = 'acme-omega' AND publishes_key = ?",
            (SHARED,),
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    assert changed == 1, "the injected fault must really land on exactly one manifest row"

    result = _sequence(root)
    assert result.exit_code == ExitCode.UNRESOLVED_FINDINGS, result.output

    rows = _collisions(root)
    assert len(rows) == 1, f"the blocking row must be persisted, not only reported: {rows}"
    key, repo_ids, severity, resolution = rows[0]
    assert key == SHARED
    assert json.loads(repo_ids) == ["acme-alpha", "acme-omega"]
    assert severity == "error"
    assert resolution is None, "a blocking row is error AND unresolved (CollisionReport.blocking)"


# =======================================================================================
# the control
# =======================================================================================

NO_CONTEST: Mapping[str, Mapping[str, str]] = {
    "acme-alpha": {"package.json": _pkg("@acme/alpha"), "src/index.ts": "export const a = 1;\n"},
    "acme-omega": {"package.json": _pkg("@acme/omega"), "src/index.ts": "export const o = 1;\n"},
}


def test_a_fleet_with_no_contest_writes_no_coordinate_row_and_still_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control. A detector that fires on every fleet is worse than no detector at all.

    Same shape as the fixtures above — two real repos, two real `package.json`s, the same scan
    and the same sequence — differing only in that the two names do not collide. If this went
    red, every assertion in this file would be measuring the audit's willingness to emit a row
    rather than its ability to find a contest.
    """
    root = _workspace(tmp_path, monkeypatch, NO_CONTEST)
    _scan_then_sequence(root)

    assert _collisions(root) == []
    assert _query(root, "SELECT COUNT(*) FROM collisions")[0][0] == 0
