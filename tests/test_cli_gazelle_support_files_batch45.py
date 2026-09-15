"""Round VIII §15.1 item 3, Wave 7.5 batch 45 (group G6a) — direct, standalone unit tests of
`src/fleet/cli.py`'s gazelle/support-file resolution helpers:

`_coordinate_for_key`, `_stub_workspace_deps`, `_union_workspace_deps`, `_stub_package_files`,
`_union_support_files`, `_owned_coordinate_keys`, `_manifest_paths`, `_external_coordinates`,
`_existing_paths`, `_resolve_support_files`, `_carried`, `_run_resolution`, `_read_if_file`,
`_resolved_support_files`, `_scratch_build_files`, `_assemble_gazelle_scratch`, `_gazelle_argv`,
`_run_gazelle`, `_gazelle_files`, `_fleet_support_files`, `_module_inputs`,
`_check_root_file_domain`, `_note_finding`, `_monorepo_checkout`.

Every one of these is already reached, extensively, by the real-Bazel/real-gazelle/real-resolver
fixtures in `tests/test_build_e2e.py` and by the stub-lifecycle fixtures in
`tests/test_stub_resolution_task79.py` — so each test below targets specifically the branch that
survey established those heavier suites do NOT exercise (a defensive raise, a multi-candidate
search order, a cross-group isolation, an unused-in-production keyword), built on the cheapest
fixture that can reach it: a bare `fresh_db`-schema SQLite file for the DB-backed functions
(mirroring `tests/test_stub_resolution_task79.py`'s own "Round VIII ... batch 30" convention), a
plain `tmp_path` tree for the pure filesystem functions, and hand-built `cli._BuildPlan`/real-
ecosystem-adapter fixtures (reusing `tests/test_build_e2e.py`'s own `_root_file_plan`/
`FakeResolver`) for the driver-level functions — never the full `fleet`/`monorepo`/`filter_repo`
e2e machinery those files exist to amortize.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import uuid4

import aiosqlite
import pytest

from fleet import cli, ecosystems
from fleet.bazel.layout import stub_dest
from fleet.models.build import (
    BuildTarget,
    BuildUnit,
    GazelleConfig,
    Resolution,
    SupportFile,
    WorkspaceDep,
)
from fleet.models.enums import Ecosystem
from fleet.models.repo import Coordinate
from fleet.settings import FleetSettings
from fleet.state.db import StateWriter, connect_ro
from fleet.util.proc import ProcResult
from tests.test_build_e2e import _root_file_plan
from tests.test_cli import fresh_db, seed_run, write_config

RUN: Final = "10000000-1000-4000-8000-000000000045"
_STAMP: Final = "2026-09-14T00:00:00+00:00"


def _plan(
    *,
    repo_id: str,
    dest: str,
    worktree: Path,
    unit: BuildUnit,
    targets: tuple[BuildTarget, ...] = (),
    workspace_deps: tuple[WorkspaceDep, ...] = (),
    workspace_files: tuple[SupportFile, ...] = (),
    package_files: tuple[SupportFile, ...] = (),
    root_targets: tuple[BuildTarget, ...] = (),
    gazelle: GazelleConfig | None = None,
    adapter_name: str = "fake",
) -> cli._BuildPlan:
    """A minimal `_BuildPlan`, everything not relevant to a given test held at an inert default —
    the same shape as `tests/test_build_e2e.py::_root_file_plan` and
    `tests/test_workers_build.py::_a_build_plan`, but not tied to a real adapter's own targets."""
    return cli._BuildPlan(
        repo_id=repo_id,
        dest=dest,
        worktree=worktree,
        integration_ref="refs/fleet/test/integration/0",
        integration_sha="a" * 40,
        merge_sha="b" * 40,
        source_sha="c" * 40,
        already_ingested=False,
        unit=unit,
        targets=targets,
        workspace_deps=workspace_deps,
        toolchains=(),
        workspace_files=workspace_files,
        package_files=package_files,
        root_targets=root_targets,
        requirements=(),
        gazelle=gazelle,
        baseline_test_count=0,
        baseline_ok=None,
        adapter_name=adapter_name,
        adapter_degraded=False,
    )


def _seed_run_and_repos(db_path: Path, *, repos: tuple[str, ...]) -> None:
    seed_run(db_path, run_id=RUN, repos=repos)


def _insert_coordinate(
    db_path: Path,
    *,
    coord_key: str,
    ecosystem: str,
    group: str,
    name: str,
    owner_repo_id: str | None,
) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO coordinates (coord_key, ecosystem, grp, name, owner_repo_id, version, "
            "                         first_seen_at) VALUES (?, ?, ?, ?, ?, NULL, ?)",
            (coord_key, ecosystem, group, name, owner_repo_id, _STAMP),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_stub(
    db_path: Path,
    *,
    coord_key: str,
    consumer_repo_id: str,
    provider_repo_id: str,
    fidelity: str,
    pinned_version: str | None,
) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO stubs (stub_id, run_id, repo_id, stub_coord_key, consumer_repo_id, "
            "                   provider_repo_id, pinned_version, bazel_label, state, "
            "                   stub_fidelity, resolved_at, state_changed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, NULL, ?, ?)",
            (
                str(uuid4()),
                RUN,
                consumer_repo_id,
                coord_key,
                consumer_repo_id,
                provider_repo_id,
                pinned_version,
                cli._internal_label(stub_dest(coord_key)),
                fidelity,
                _STAMP,
                _STAMP,
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def _ro[T](
    db_path: Path, fn: Callable[[aiosqlite.Connection], Awaitable[T]]
) -> T:
    conn = await connect_ro(db_path)
    try:
        return await fn(conn)
    finally:
        await conn.close()


# ======================================================================================
# _coordinate_for_key
# ======================================================================================


def test_coordinate_for_key_raises_loudly_when_coordinates_has_no_matching_row(
    tmp_path: Path,
) -> None:
    """Own docstring (Rule 11): a `stubs.stub_coord_key` is always a real `edges.dst_coord_key`,
    and `coordinates` has no row for it only if an upstream invariant already broke. Every real
    caller (`_stub_workspace_deps`, `_stub_package_files`) is exercised ONLY through fixtures that
    seed a matching `coordinates` row first (the `acme-commons-java` maven scan in
    `tests/test_build_e2e.py`), so this raise has never fired under test."""
    db_path = fresh_db(tmp_path / "state" / "fleet.db")
    _seed_run_and_repos(db_path, repos=("acme-consumer",))

    async def _call(conn):
        return await cli._coordinate_for_key(conn, "npm::ghost-package")

    with pytest.raises(ValueError, match="npm::ghost-package"):
        asyncio.run(_ro(db_path, _call))


# ======================================================================================
# _stub_workspace_deps — review finding M1's explicit fidelity predicate
# ======================================================================================


def test_stub_workspace_deps_excludes_an_empty_failing_stub_even_alongside_a_qualifying_one(
    tmp_path: Path,
) -> None:
    """M1 (own docstring): "`EMPTY_FAILING` rows ... are excluded by an EXPLICIT
    `stub_fidelity = 'PUBLISHED_ARTIFACT'` predicate, not merely by `pinned_version IS NOT NULL`".
    `tests/test_build_e2e.py`'s own e2e proof of this function (§37 Leg 2) seeds exactly ONE
    PUBLISHED_ARTIFACT row and never an EMPTY_FAILING sibling in the same run, so the predicate's
    OTHER half — that a second, EMPTY_FAILING row for the SAME run does not also render — has
    never been checked directly."""
    db_path = fresh_db(tmp_path / "state" / "fleet.db")
    _seed_run_and_repos(
        db_path, repos=("acme-commons-java", "acme-abandoned-lib", "acme-consumer")
    )
    _insert_coordinate(
        db_path,
        coord_key="maven:com.acme:commons",
        ecosystem="maven",
        group="com.acme",
        name="commons",
        owner_repo_id="acme-commons-java",
    )
    _insert_stub(
        db_path,
        coord_key="maven:com.acme:commons",
        consumer_repo_id="acme-consumer",
        provider_repo_id="acme-commons-java",
        fidelity="PUBLISHED_ARTIFACT",
        pinned_version="9.9.9",
    )
    # `pinned_version` is deliberately NON-NULL here (schema.sql's CHECK permits it for
    # EMPTY_FAILING — only PUBLISHED_ARTIFACT is forbidden a NULL one) precisely so this fixture
    # cannot be satisfied by a weaker `pinned_version is None` check alone (M1's own warning): a
    # `coordinates` row is seeded for it too, so a regression that stops checking `stub_fidelity`
    # would successfully render a SECOND dep from it rather than merely erroring.
    _insert_coordinate(
        db_path,
        coord_key="npm::acme-abandoned-lib",
        ecosystem="npm",
        group="",
        name="acme-abandoned-lib",
        owner_repo_id="acme-abandoned-lib",
    )
    _insert_stub(
        db_path,
        coord_key="npm::acme-abandoned-lib",
        consumer_repo_id="acme-consumer",
        provider_repo_id="acme-abandoned-lib",
        fidelity="EMPTY_FAILING",
        pinned_version="1.0.0",
    )
    ecosystems.discover()

    async def _call(conn):
        return await cli._stub_workspace_deps(conn, RUN)

    deps = asyncio.run(_ro(db_path, _call))
    assert len(deps) == 1, deps
    assert deps[0].coordinate.name == "commons", deps
    assert deps[0].resolved_version == "9.9.9", deps
    assert all("abandoned" not in d.coordinate.name for d in deps), deps


# ======================================================================================
# _union_workspace_deps
# ======================================================================================


def test_union_workspace_deps_prefers_a_real_declaration_over_a_stub_ghost() -> None:
    """Own docstring: "an ordinary dependency on the same coordinate ... wins over the stub's,
    since the real declaration is strictly more informative" — never exercised anywhere: today no
    real declaration and a stub declaration ever collide (a stub's coordinate is internally
    owned), so this precedence has no e2e fixture to fire it."""
    coord = Coordinate(ecosystem=Ecosystem.MAVEN, group="com.acme", name="commons")
    real = WorkspaceDep(
        ruleset="rules_jvm_external",
        extension="maven.install",
        coordinate=coord,
        resolved_version="1.2.0",
        repo_name="maven",
    )
    stub = WorkspaceDep(
        ruleset="rules_jvm_external",
        extension="maven.install",
        coordinate=coord,
        resolved_version="9.9.9",
        repo_name="maven",
    )
    merged = cli._union_workspace_deps([real], [stub])
    assert merged == [real], merged
    assert merged[0].resolved_version == "1.2.0", "the real declaration, never the stub's pin"


# ======================================================================================
# _stub_package_files — the `len(labels) != 1` fail-loud guard
# ======================================================================================


def test_stub_package_files_raises_loudly_when_the_adapter_names_more_than_one_external_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Own docstring (Rule 11): "if `len(labels) != 1`: raise ... expected exactly one label to
    alias". Every shipped adapter's `external_labels()` returns exactly one label for a
    single-external-coordinate unit, so nothing in the real fixtures can ever construct the
    violation this guards — reached here with a fake adapter that returns two."""
    db_path = fresh_db(tmp_path / "state" / "fleet.db")
    _seed_run_and_repos(db_path, repos=("acme-provider", "acme-consumer"))
    _insert_coordinate(
        db_path,
        coord_key="npm::acme-widget",
        ecosystem="npm",
        group="",
        name="acme-widget",
        owner_repo_id="acme-provider",
    )
    _insert_stub(
        db_path,
        coord_key="npm::acme-widget",
        consumer_repo_id="acme-consumer",
        provider_repo_id="acme-provider",
        fidelity="PUBLISHED_ARTIFACT",
        pinned_version="2.0.0",
    )

    class _TwoLabelAdapter:
        name = "npm"

        def external_labels(self, unit):
            return ["@npm//:acme-widget", "@npm//:acme-widget-extra"]

    monkeypatch.setattr(cli.ecosystems, "for_ecosystem", lambda eco: _TwoLabelAdapter())

    async def _call(conn):
        return await cli._stub_package_files(conn, RUN)

    with pytest.raises(ValueError, match="external_labels"):
        asyncio.run(_ro(db_path, _call))


# ======================================================================================
# _union_support_files
# ======================================================================================


def test_union_support_files_prefers_a_real_file_over_a_stub_package_file() -> None:
    """Own docstring: "a real file at the same path wins over the stub's" — disclosed as "not
    reachable under any of the five shipped adapters today" (none names `third_party` as its
    `monorepo_dir`), so the precedence itself is untested anywhere; a fixture can still construct
    the collision directly since the function does not know what a path convention is."""
    real = SupportFile(path="third_party/npm/BUILD.bazel", content="real")
    stub = SupportFile(path="third_party/npm/BUILD.bazel", content="stub")
    merged = cli._union_support_files([real], [stub])
    assert merged == [real], merged


# ======================================================================================
# _owned_coordinate_keys
# ======================================================================================


def test_owned_coordinate_keys_excludes_external_null_owner_rows(tmp_path: Path) -> None:
    """Own docstring: "read from `coordinates.owner_repo_id` rather than from this invocation's
    evidence" — the NULL-owner (external) exclusion is the query's whole point and is untested
    directly anywhere (every e2e fixture that reaches this reads it as one opaque frozenset)."""
    db_path = fresh_db(tmp_path / "state" / "fleet.db")
    _seed_run_and_repos(db_path, repos=("acme-commons-java",))
    _insert_coordinate(
        db_path,
        coord_key="maven:com.acme:commons",
        ecosystem="maven",
        group="com.acme",
        name="commons",
        owner_repo_id="acme-commons-java",
    )
    _insert_coordinate(
        db_path,
        coord_key="maven:com.google.guava:guava",
        ecosystem="maven",
        group="com.google.guava",
        name="guava",
        owner_repo_id=None,
    )

    async def _call(conn):
        return await cli._owned_coordinate_keys(conn)

    owned = asyncio.run(_ro(db_path, _call))
    assert owned == frozenset({"maven:com.acme:commons"}), owned


# ======================================================================================
# _manifest_paths
# ======================================================================================


def test_manifest_paths_excludes_the_unknown_adapter_and_a_parse_error_row(
    tmp_path: Path,
) -> None:
    """Own docstring: "`adapter = 'unknown'` is the synthetic ... catch-all" and (implicitly, the
    `WHERE` clause's other half) a manifest that failed to parse this run — neither exclusion is
    checked directly anywhere; every e2e fixture reaching this function has a clean, fully-parsed
    manifest set."""
    db_path = fresh_db(tmp_path / "state" / "fleet.db")
    _seed_run_and_repos(db_path, repos=("acme-commons-java",))
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO manifests (repo_id, path, ecosystem, adapter, sha256, parse_error, "
            "                       parsed_at) VALUES (?, ?, ?, ?, ?, NULL, ?)",
            ("acme-commons-java", "pom.xml", "maven", "maven", "a" * 64, _STAMP),
        )
        conn.execute(
            "INSERT INTO manifests (repo_id, path, ecosystem, adapter, sha256, parse_error, "
            "                       parsed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "acme-commons-java",
                "build.gradle",
                "gradle",
                "gradle",
                "b" * 64,
                "build.gradle: malformed",
                _STAMP,
            ),
        )
        conn.execute(
            "INSERT INTO manifests (repo_id, path, ecosystem, adapter, sha256, parse_error, "
            "                       parsed_at) VALUES (?, ?, ?, ?, ?, NULL, ?)",
            ("acme-commons-java", "README.md", "unknown", "unknown", "c" * 64, _STAMP),
        )
        conn.commit()
    finally:
        conn.close()

    async def _call(conn):
        return await cli._manifest_paths(conn)

    paths = asyncio.run(_ro(db_path, _call))
    assert paths == {"acme-commons-java": ("pom.xml",)}, paths


# ======================================================================================
# _external_coordinates — the `optional` skip
# ======================================================================================


def test_external_coordinates_skips_an_optional_dependency(tmp_path: Path) -> None:
    """Own docstring: "if raw.optional: continue" — never exercised: no test in the suite ever
    writes a manifest section that sets `RawDependency.optional=True` and then reads it back
    through `_external_coordinates` (npm's `optionalDependencies`, `manifests/npm.py`)."""
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "acme-app",
                "dependencies": {"left-pad": "^1.0.0"},
                "optionalDependencies": {"fsevents": "^2.0.0"},
            }
        ),
        encoding="utf-8",
    )
    coords = cli._external_coordinates(tmp_path, ["package.json"], frozenset())
    names = {c.name for c in coords}
    assert names == {"left-pad"}, names
    assert "fsevents" not in names, names


# ======================================================================================
# _existing_paths
# ======================================================================================


def test_existing_paths_filters_missing_entries_and_preserves_order(tmp_path: Path) -> None:
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    (tmp_path / "d.txt").write_text("d", encoding="utf-8")
    result = cli._existing_paths(tmp_path, ["a.txt", "b.txt", "c.txt", "d.txt"])
    assert result == ["b.txt", "d.txt"], result


# ======================================================================================
# _resolve_support_files — the multi-worktree search order
# ======================================================================================


def test_resolve_support_files_finds_a_carry_candidate_in_a_later_worktree(
    tmp_path: Path,
) -> None:
    """Own docstring: "`worktree` may be several ... searched in the order given" — every existing
    caller of `_resolve_support_files` in the suite passes exactly one worktree
    (`tests/test_workers_build.py`'s own direct test included), so the fan-out across several
    trees — the entire reason the parameter accepts a sequence at all (ADR-0055 wave snapshots) —
    has never been driven directly."""
    wt1 = tmp_path / "wt1"
    wt2 = tmp_path / "wt2"
    wt1.mkdir()
    wt2.mkdir()
    real = "# real pnpm-lock.yaml\nlockfileVersion: '9.0'\n"
    (wt2 / "pnpm-lock.yaml").write_text(real, encoding="utf-8")
    declared = [
        SupportFile(path="pnpm-lock.yaml", carry_from=["pnpm-lock.yaml"], content="GENERATED\n")
    ]
    resolved = cli._resolve_support_files([wt1, wt2], declared)
    assert len(resolved) == 1
    assert resolved[0].content == real, resolved[0].content


# ======================================================================================
# _carried
# ======================================================================================


def test_carried_is_true_only_once_a_later_worktree_actually_has_the_file(
    tmp_path: Path,
) -> None:
    wt1 = tmp_path / "wt1"
    wt2 = tmp_path / "wt2"
    wt1.mkdir()
    wt2.mkdir()
    support = SupportFile(path="Cargo.lock", carry_from=["Cargo.lock"], content="floor")
    assert cli._carried([wt1, wt2], support) is False
    (wt2 / "Cargo.lock").write_text("real", encoding="utf-8")
    assert cli._carried([wt1, wt2], support) is True


# ======================================================================================
# _run_resolution — the empty-content pre-check (fires before the resolver ever runs)
# ======================================================================================


def test_run_resolution_refuses_before_invoking_the_resolver_when_an_input_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Own docstring: "an input with nothing behind it" is one of the loud failure modes listed —
    every existing `_run_resolution` test drives a REAL resolver invocation (real or `FakeResolver`
    argv), so this pre-check, which never reaches the runner seam at all, has no direct test.

    A `RESOLVER_RUNNER` is installed (never actually invoked when this check does its job) purely
    so that a mutation which disables the pre-check cannot fall through to a REAL subprocess —
    it would then hit the post-resolver empty-lock check instead, with a message that does not
    name `requirements.in`, which is exactly the discriminator this test needs.
    """

    class _NeverActuallyRun:
        async def __call__(self, argv, *, cwd=None, env=None, deadline=None, timeout_s=None):
            return ProcResult(
                argv=tuple(argv),
                exit_code=0,
                stdout_tail="",
                stderr_tail="",
                duration_ms=1,
                timed_out=False,
                cwd=cwd,
            )

    monkeypatch.setattr(cli, "RESOLVER_RUNNER", _NeverActuallyRun())
    plan = Resolution(
        lock_path="requirements.lock",
        argv=["uv", "pip", "compile"],
        inputs=[SupportFile(path="requirements.in", content="")],
    )
    with pytest.raises(cli.DependencyResolutionError, match=r"requirements\.in"):
        asyncio.run(
            cli._run_resolution(
                plan, repo_id="acme-app-py", worktrees=[], scratch=tmp_path / "scratch"
            )
        )


# ======================================================================================
# _read_if_file
# ======================================================================================


def test_read_if_file_returns_none_for_a_missing_path_and_text_for_a_present_one(
    tmp_path: Path,
) -> None:
    assert cli._read_if_file(tmp_path / "nope.txt") is None
    present = tmp_path / "there.txt"
    present.write_text("hi", encoding="utf-8")
    assert cli._read_if_file(present) == "hi"


# ======================================================================================
# _resolved_support_files — the resolution()/workspace_files() consistency guard
# ======================================================================================


def test_resolved_support_files_raises_when_the_resolution_names_a_path_workspace_files_does_not(
    tmp_path: Path,
) -> None:
    """Own docstring (Rule 11): "declares a resolver for `plan.lock_path`, which is not one of the
    files it says its MODULE.bazel tags name" — a same-adapter self-contradiction no shipped
    adapter commits (every real `resolution()` lock_path is one of its own `workspace_files()`
    paths by construction), reached here with a fake adapter that disagrees with itself."""

    class _InconsistentAdapter:
        name = "fake"

        def workspace_files(self, units):
            return [SupportFile(path="other.txt", content="x")]

        def resolution(self, units):
            return Resolution(lock_path="lock.txt", argv=["true"])

    unit = BuildUnit(unit_id="acme-app", ecosystem=Ecosystem.PYPI, dest="py/acme_app")
    with pytest.raises(cli.DependencyResolutionError, match=r"lock\.txt"):
        asyncio.run(
            cli._resolved_support_files(
                _InconsistentAdapter(),  # type: ignore[arg-type]  # duck-typed fake, not a full EcosystemAdapter
                [unit],
                repo_id="acme-app",
                # A NON-empty `worktrees` matters: with `worktrees=[]`, disabling this guard
                # would fall through to `_carried([], lock)`, whose `any(... for _ in [])` never
                # even evaluates `lock.carry_from` — silently `False` rather than an error — and
                # from there into a REAL `_run_resolution(["true"], ...)` that coincidentally also
                # raises a `DependencyResolutionError` mentioning `lock.txt` (its own, unrelated,
                # empty-lock check), defeating this test's discriminating power.
                worktrees=[tmp_path],
                scratch=tmp_path / "scratch",
            )
        )


# ======================================================================================
# _scratch_build_files — the non-UTF-8 file is omitted, not mangled
# ======================================================================================


def test_scratch_build_files_omits_a_build_file_that_is_not_valid_utf8(tmp_path: Path) -> None:
    """Own docstring: "A file that is not readable UTF-8 is omitted rather than mangled" — never
    driven directly; every real-gazelle fixture's generator output is UTF-8 BUILD text."""
    good = tmp_path / "go" / "commons" / "BUILD.bazel"
    good.parent.mkdir(parents=True)
    good.write_text("go_library(name = \"commons\")\n", encoding="utf-8")
    bad = tmp_path / "go" / "svc" / "BUILD.bazel"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"\xff\xfe\x00bad-bytes")

    out = cli._scratch_build_files(tmp_path)
    assert set(out) == {"go/commons/BUILD.bazel"}, out


# ======================================================================================
# _assemble_gazelle_scratch — an adapter-declared MODULE.bazel is never overwritten
# ======================================================================================


def test_assemble_gazelle_scratch_never_overwrites_an_adapter_declared_module_bazel(
    tmp_path: Path,
) -> None:
    """Own docstring: "A root `MODULE.bazel`, only if the adapter did not declare one" — every
    real-generator fixture in `tests/test_build_e2e.py` is Go-only, and no shipped adapter's
    `workspace_files()` ever names `MODULE.bazel` (real Go plans never carry one), so the
    "already present, do not synthesize" half of this branch has no existing coverage."""
    worktree = tmp_path / "src"
    (worktree / "go" / "commons").mkdir(parents=True)
    (worktree / "go" / "commons" / "main.go").write_text("package main\n", encoding="utf-8")
    declared = SupportFile(path="MODULE.bazel", content="module(name = 'acme_monorepo')\n")
    plan = _plan(
        repo_id="acme-commons-go",
        dest="go/commons",
        worktree=worktree,
        unit=BuildUnit(unit_id="acme-commons-go", ecosystem=Ecosystem.GO, dest="go/commons"),
        workspace_files=(declared,),
    )
    scratch = tmp_path / "scratch"
    cli._assemble_gazelle_scratch(scratch, [plan])
    assert (scratch / "MODULE.bazel").read_text(encoding="utf-8") == declared.content


# ======================================================================================
# _gazelle_argv — disagreeing flags across the group is a loud refusal
# ======================================================================================


def test_gazelle_argv_refuses_when_two_plans_of_one_group_declare_different_flags(
    tmp_path: Path,
) -> None:
    """Own docstring (Rule 7): "Every unit of the group must declare the identical flags ... a
    disagreement has no correct resolution and is refused loudly rather than averaged" — every
    real-generator fixture's Go units share one `GazelleConfig`, so two DIFFERING configs in one
    group's argv build has never been driven."""
    plan_a = _plan(
        repo_id="acme-commons-go",
        dest="go/commons",
        worktree=tmp_path,
        unit=BuildUnit(unit_id="acme-commons-go", ecosystem=Ecosystem.GO, dest="go/commons"),
        gazelle=GazelleConfig(args=["-go_naming_convention=import"]),
    )
    plan_b = _plan(
        repo_id="acme-svc-go",
        dest="go/svc",
        worktree=tmp_path,
        unit=BuildUnit(unit_id="acme-svc-go", ecosystem=Ecosystem.GO, dest="go/svc"),
        gazelle=GazelleConfig(args=["-go_naming_convention=go_default_library"]),
    )
    with pytest.raises(cli.BuildFileGenerationError, match="different generator flags"):
        cli._gazelle_argv("gazelle", tmp_path / "scratch", [plan_a, plan_b])


# ======================================================================================
# _run_gazelle — a file written outside every unit's dest is a loud refusal
# ======================================================================================


def test_run_gazelle_refuses_loudly_when_the_generator_writes_outside_every_dest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Own docstring: "A file the generator wrote outside every unit's `dest` is a loud refusal,
    not a dropped file" — the real vendored generator never does this (every real-generator test
    in `tests/test_build_e2e.py` exercises only files inside a planned `dest`), so this branch is
    reached here with a fake `GAZELLE_RUNNER` that deliberately writes a stray file."""
    worktree = tmp_path / "src"
    (worktree / "go" / "commons").mkdir(parents=True)
    (worktree / "go" / "commons" / "main.go").write_text("package main\n", encoding="utf-8")
    plan = _plan(
        repo_id="acme-commons-go",
        dest="go/commons",
        worktree=worktree,
        unit=BuildUnit(unit_id="acme-commons-go", ecosystem=Ecosystem.GO, dest="go/commons"),
    )

    class _StrayWriter:
        async def __call__(self, argv, *, cwd=None, env=None, deadline=None, timeout_s=None):
            assert cwd is not None
            (cwd / "stray").mkdir(parents=True, exist_ok=True)
            (cwd / "stray" / "BUILD.bazel").write_text("stray\n", encoding="utf-8")
            return ProcResult(
                argv=tuple(argv),
                exit_code=0,
                stdout_tail="",
                stderr_tail="",
                duration_ms=1,
                timed_out=False,
                cwd=cwd,
            )

    monkeypatch.setattr(cli, "GAZELLE_RUNNER", _StrayWriter())
    with pytest.raises(cli.BuildFileGenerationError, match=r"stray/BUILD\.bazel"):
        asyncio.run(cli._run_gazelle([plan], binary="gazelle", scratch=tmp_path / "scratch"))


# ======================================================================================
# _gazelle_files — an assembly OSError is wrapped and attributed to the whole group
# ======================================================================================


def test_gazelle_files_wraps_an_assembly_oserror_and_attributes_it_to_every_repo_of_the_group(
    tmp_path: Path,
) -> None:
    """Own docstring: "except (BuildFileGenerationError, OSError) as exc: ... attributed to every
    repo of that ecosystem" — every real-generator fixture assembles cleanly, so the `OSError`
    (non-`BuildFileGenerationError`) half of that `except` clause, wrapping a scratch-assembly
    failure rather than a generator failure, is untested."""
    ecosystems.discover()
    write_config(tmp_path / "ws")
    settings = FleetSettings.load(tmp_path / "ws" / "config")

    good_worktree = tmp_path / "wt-commons"
    (good_worktree / "go" / "commons").mkdir(parents=True)
    (good_worktree / "go" / "commons" / "main.go").write_text("package main\n", encoding="utf-8")

    plans = {
        "acme-commons-go": _plan(
            repo_id="acme-commons-go",
            dest="go/commons",
            worktree=good_worktree,
            unit=BuildUnit(unit_id="acme-commons-go", ecosystem=Ecosystem.GO, dest="go/commons"),
        ),
        "acme-svc-go": _plan(
            repo_id="acme-svc-go",
            dest="go/svc",
            worktree=tmp_path / "wt-missing",  # never created -> shutil.copytree raises OSError
            unit=BuildUnit(unit_id="acme-svc-go", ecosystem=Ecosystem.GO, dest="go/svc"),
        ),
    }

    files, failures = asyncio.run(
        cli._gazelle_files(plans, settings=settings, run_id="run-assembly-fail")
    )
    assert files == {}, files
    assert set(failures) == {"acme-commons-go", "acme-svc-go"}, failures
    failure = failures["acme-commons-go"]
    assert failures["acme-svc-go"] is failure, "one assembly attempt, one shared verdict"
    assert isinstance(failure, cli.BuildFileGenerationError), type(failure)
    assert "scratch tree" in str(failure), failure


# ======================================================================================
# _fleet_support_files — a DependencyResolutionError in one group leaves a sibling untouched
# ======================================================================================


def test_a_dependency_resolution_failure_in_one_group_does_not_cost_a_sibling_groups_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`test_an_unrenderable_coordinate_is_contained_to_its_own_ecosystems_repos` proves cross-
    group isolation for the sibling `except ecosystems.AdapterCoordinateError` branch, and
    `test_a_resolver_failure_is_attributed_to_every_repo_of_the_shared_ecosystem` proves the
    `except DependencyResolutionError` branch's WITHIN-group attribution — but no existing test
    combines a FAILING resolver group with a SUCCEEDING sibling group in the same
    `_fleet_support_files` call, so the `continue`'s "the rest of the fleet still gets its files"
    half of this specific except clause has never been checked (CLAUDE.md: "don't assume
    symmetry" across a driver's several near-identical except clauses)."""
    ecosystems.discover()

    class BrokenResolver:
        async def __call__(self, argv, *, cwd=None, env=None, deadline=None, timeout_s=None):
            return ProcResult(
                argv=tuple(argv),
                exit_code=2,
                stdout_tail="",
                stderr_tail="no solution found",
                duration_ms=1,
                timed_out=False,
                cwd=cwd,
            )

    monkeypatch.setattr(cli, "RESOLVER_RUNNER", BrokenResolver())
    write_config(tmp_path / "ws")
    settings = FleetSettings.load(tmp_path / "ws" / "config")

    rust_worktree = tmp_path / "rust-widget"
    (rust_worktree / "rust" / "widget").mkdir(parents=True)
    real_lock = "# real, already-resolved Cargo.lock\nversion = 3\n"
    (rust_worktree / "rust" / "widget" / "Cargo.lock").write_text(real_lock, encoding="utf-8")

    py_worktree = tmp_path / "py-app"
    py_worktree.mkdir()

    plans = {
        "acme-widget-rs": _root_file_plan(
            BuildUnit(
                unit_id="acme-widget-rs",
                ecosystem=Ecosystem.CARGO,
                dest="rust/widget",
                srcs=["lib.rs"],
                external_coordinates=[
                    Coordinate(ecosystem=Ecosystem.CARGO, name="serde", version_spec="1.0")
                ],
            ),
            rust_worktree,
            "rust",
        ),
        "acme-app-py": _root_file_plan(
            BuildUnit(
                unit_id="acme-app-py",
                ecosystem=Ecosystem.PYPI,
                dest="py/acme_app_py",
                srcs=["acme_app_py/__init__.py"],
                external_coordinates=[
                    Coordinate(ecosystem=Ecosystem.PYPI, name="requests", version_spec=">=2.31")
                ],
            ),
            py_worktree,
            "py",
        ),
    }

    files, failures = asyncio.run(
        cli._fleet_support_files(plans, settings=settings, run_id="run-mixed")
    )

    assert set(files) == {"acme-widget-rs"}, files
    resolved = {support.path: support.content for support in files["acme-widget-rs"]}
    assert resolved["Cargo.lock"] == real_lock, resolved

    assert set(failures) == {"acme-app-py"}, failures
    assert isinstance(failures["acme-app-py"], cli.DependencyResolutionError), failures


# ======================================================================================
# _module_inputs — a native rule's `load_from=None` contributes nothing to `module_targets`
# ======================================================================================


def test_module_inputs_excludes_a_native_rules_load_from_none_from_module_targets(
    tmp_path: Path,
) -> None:
    """Own docstring: "It needs only their `load_from`, so one representative target per distinct
    label is kept" — implying a target with NO `load_from` (a native rule) contributes nothing,
    which is never checked directly: every e2e fixture's assertions read the RENDERED
    `MODULE.bazel` text, never `_module_inputs`'s own third return value."""
    unit = BuildUnit(unit_id="acme-app-py", ecosystem=Ecosystem.PYPI, dest="py/acme_app_py")
    native = BuildTarget(package="py/acme_app_py", name="lib", rule="py_library", load_from=None)
    loaded = BuildTarget(
        package="py/acme_app_py",
        name="bin",
        rule="js_binary",
        load_from="@aspect_rules_js//js:defs.bzl",
    )
    plan = _plan(
        repo_id="acme-app-py",
        dest="py/acme_app_py",
        worktree=tmp_path,
        unit=unit,
        targets=(native, loaded),
    )
    _, _, _, module_targets, _ = cli._module_inputs({"acme-app-py": plan})
    assert module_targets == [loaded], module_targets


# ======================================================================================
# _check_root_file_domain — the CONTAINMENT half (RootFileDomainError), never fired under test
# ======================================================================================


async def test_check_root_file_domain_raises_when_a_dispatched_worktree_lacks_a_domain_dest(
    tmp_path: Path,
) -> None:
    """`tests/test_prepare_before_admit.py` fires the sibling COVERAGE half
    (`RootFileDomainDriftError`) through a real `fleet build`; nothing in the suite ever fires the
    CONTAINMENT half (`RootFileDomainError`, own docstring: "the other half") — confirmed by
    `tests/test_build_e2e.py:2257`'s own comment, "no `RootFileDomainError` over a `dest` that
    never materialized", which only asserts the ABSENCE of this error, never its presence."""
    wt_a = tmp_path / "wt-a"
    (wt_a / "py" / "a").mkdir(parents=True)
    plans = {
        "repo-a": _plan(
            repo_id="repo-a",
            dest="py/a",
            worktree=wt_a,
            unit=BuildUnit(unit_id="repo-a", ecosystem=Ecosystem.PYPI, dest="py/a"),
        ),
        "repo-b": _plan(
            repo_id="repo-b",
            dest="py/b",
            worktree=tmp_path / "wt-b",
            unit=BuildUnit(unit_id="repo-b", ecosystem=Ecosystem.PYPI, dest="py/b"),
        ),
    }
    domain = {"repo-a": "py/a", "repo-b": "py/b"}

    with pytest.raises(cli.RootFileDomainError) as exc_info:
        await cli._check_root_file_domain(plans, ["repo-a"], domain=domain)
    assert exc_info.value.missing == {"repo-a": ("py/b",)}, exc_info.value.missing


# ======================================================================================
# _note_finding — the `fingerprint_parts` override, never passed by any caller in this tree
# ======================================================================================


async def test_note_finding_custom_fingerprint_parts_keeps_two_rows_distinct(
    tmp_path: Path,
) -> None:
    """Every call site in `src/fleet/cli.py` (six of them) omits `fingerprint_parts`, relying on
    the default `(run_id, repo_id or "", kind)` — none ever supplies the "finer key" the own
    docstring's `ContractBindingUnavailable`/precedent paragraph exists to support, so the
    override branch (`parts = fingerprint_parts if fingerprint_parts is not None else ...`) has
    literally never executed anywhere in this codebase before this test."""
    db_path = fresh_db(tmp_path / "state" / "fleet.db")
    _seed_run_and_repos(db_path, repos=("acme-consumer",))
    now = datetime(2026, 9, 14, tzinfo=UTC)

    async with StateWriter(db_path, owner="test-writer") as writer:
        await cli._note_finding(
            writer,
            RUN,
            None,
            kind="ContractBindingUnavailable",
            payload={"contract_id": "proto:acme.widget"},
            severity="warn",
            now=now,
            fingerprint_parts=(RUN, "", "ContractBindingUnavailable", "proto:acme.widget"),
        )
        await cli._note_finding(
            writer,
            RUN,
            None,
            kind="ContractBindingUnavailable",
            payload={"contract_id": "proto:acme.other"},
            severity="warn",
            now=now,
            fingerprint_parts=(RUN, "", "ContractBindingUnavailable", "proto:acme.other"),
        )

    async def _read(conn):
        return await cli._rows(
            conn,
            "SELECT payload FROM findings WHERE run_id = ? AND kind = 'ContractBindingUnavailable'",
            (RUN,),
        )

    rows = await _ro(db_path, _read)
    assert len(rows) == 2, rows
    contract_ids = {json.loads(row[0])["contract_id"] for row in rows}
    assert contract_ids == {"proto:acme.widget", "proto:acme.other"}, contract_ids


# ======================================================================================
# _monorepo_checkout — the wrong-branch-checked-out branch
# ======================================================================================


async def test_monorepo_checkout_refuses_when_the_wrong_branch_is_checked_out(
    tmp_path: Path,
) -> None:
    """`tests/test_stub_resolution_task79.py`'s and `tests/test_hoist_rollback_git.py`'s existing
    direct coverage of `_monorepo_checkout` only fires the MISSING-repo branch
    (`no git repository at ...`); nothing exercises the second `MonorepoUnavailableError` branch —
    a monorepo that exists but has some OTHER branch checked out — even though it is a distinct
    `if` with its own message."""
    from fleet.vcs.git import Git

    workspace = tmp_path / "ws"
    write_config(workspace)
    settings = FleetSettings.load(workspace / "config")

    monorepo_path = workspace.parent / "acme-monorepo"
    monorepo_path.mkdir(parents=True)
    git = Git(monorepo_path)
    await git.exec(["init", "-b", "main"])
    await git.exec(["config", "user.email", "fleet@example.invalid"])
    await git.exec(["config", "user.name", "fleet"])
    (monorepo_path / "README.md").write_text("base\n", encoding="utf-8")
    await git.exec(["add", "-A"])
    await git.commit("base commit")

    with pytest.raises(cli.MonorepoUnavailableError, match="main"):
        await cli._monorepo_checkout(settings)
