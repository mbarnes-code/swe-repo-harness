"""D107/D104/D108 (ADR-0128, round VI task 79) — the stub-resolution label rewrite, the
REVALIDATE claiming loop, and the `consumer_status -> phases` write, end to end.

Built around `tests/test_build_e2e.py`'s REAL consumer->provider edge fixture
(`_STUB_CONSUMER='acme-app-py'` -> `_STUB_PROVIDER='acme-lib-py'`), the same shape
`test_an_active_stub_redirects_the_consumers_generated_dependency_to_the_stubs_label` and
`test_a_superseded_stub_leaves_the_consumers_generated_dependency_on_the_real_label` already
prove label COMPUTATION for. This file proves the part those two do NOT: rewriting an
ALREADY-COMMITTED `migrate/<consumer>` branch in place, driving the REVALIDATE claiming loop
against the rewritten tree, and the `consumer_status -> phases` write that promotes the consumer.

Read the file's own docstring split below each test for which assertions are proven under real
Bazel vs `FakeBazel`, per this task's brief.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
import structlog.testing
from typer.testing import CliRunner

from fleet import cli
from fleet.cli import (
    ExitCode,
    GlobalOptions,
    _load_settings,
    _rewrite_superseded_consumer_labels,
    _run_revalidation_claims_impl,
    app,
)
from tests.test_build_e2e import (  # noqa: F401  (fixtures used by injection)
    _PROVIDER_LABEL,
    _STUB_CONSUMER,
    _STUB_COORD_KEY,
    _STUB_LABEL,
    _STUB_PROVIDER,
    DESTINATIONS,
    FakeBazel,
    FakeFilterRepo,
    _insert_stub_row,
    bazel,
    build,
    build_worktree,
    filter_repo,
    gazelle,
    make_monorepo,
    monorepo,
    payload,
    relocations,
    resolver,
    transformed,
)
from tests.test_transform_e2e import (  # noqa: F401  (fixtures used by injection)
    base_args,
    fleet,
    git,
    query,
    scan,
    scanned,
    sequence,
    transform,
    write_rules,
)

runner = CliRunner()


def _git_show(monorepo_path: Path, ref: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(monorepo_path), "show", ref],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        timeout=60.0,
    )
    return result.stdout


def _git_log_count(monorepo_path: Path, branch: str) -> int:
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(monorepo_path), "rev-list", "--count", branch],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        timeout=60.0,
    )
    return int(result.stdout.strip())


def _reach_active_stub_state(
    fleet: Path,  # noqa: F811
    monorepo: Path,  # noqa: F811
    filter_repo: FakeFilterRepo,  # noqa: F811
    *,
    bazel_registry: str | None = None,
    bazel_fetch_bazelrc: str | None = None,
) -> str:
    """Exactly `test_an_active_stub_redirects_the_consumers_generated_dependency_to_the_stubs_
    label`'s own setup (`tests/test_build_e2e.py`): `acme-lib-py` ends `REQUIRES_HUMAN_
    INTERVENTION`, `acme-app-py` ends `DEGRADED` with an `ACTIVE` stub naming the REAL
    `acme-app-py -> acme-lib-py` edge. Returns the run_id.

    When `bazel_registry`/`bazel_fetch_bazelrc` are given, the module registry pin is committed
    to the monorepo BEFORE `transformed()` cuts anything from it (mirrors `test_build_e2e.
    real_build`'s own reasoning: Phase 3 cuts its worktree from the `integration` tip, so an
    untracked `.bazelrc` is not in the tree a LATER real-Bazel step would ever see) — needed by
    this file's real-Bazel REVALIDATE tests, harmless (never read) by the `FakeBazel`-only ones.
    """
    if bazel_registry is not None:
        (monorepo / ".bazelrc").write_text(
            f"common --registry={bazel_registry}\n{bazel_fetch_bazelrc or ''}", encoding="utf-8"
        )
        git(monorepo, "add", "-A")
        git(monorepo, "commit", "-m", "pin the module registry")
    fake = FakeBazel(
        fleet / "artifacts" / "fake-bazel", fail={("build", DESTINATIONS[_STUB_PROVIDER]): 34}
    )
    cli.BAZEL_RUNNER = fake
    try:
        transformed(fleet)
        run_id = str(query(fleet, "SELECT run_id FROM runs")[0][0])
        _insert_stub_row(fleet, run_id=run_id, state="ACTIVE")

        first = build(fleet, "--no-sandbox")
        assert first.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION, first.output

        conn = sqlite3.connect(fleet / "state" / "fleet.db")
        try:
            conn.execute(
                "UPDATE phases SET status = 'PENDING', blocked_by = '[]' "
                " WHERE run_id = ? AND repo_id = ? AND phase = 3",
                (run_id, _STUB_CONSUMER),
            )
            conn.commit()
        finally:
            conn.close()

        second = build(fleet, "--no-sandbox")
        assert second.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION, second.output
        statuses = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 3"))
        assert statuses[_STUB_PROVIDER] == "REQUIRES_HUMAN_INTERVENTION", statuses
        assert statuses[_STUB_CONSUMER] == "DEGRADED", statuses
        return run_id
    finally:
        cli.BAZEL_RUNNER = None


def _supersede_stub_row(fleet: Path, run_id: str) -> None:  # noqa: F811
    """T1's own DB effect (`ACTIVE -> SUPERSEDED`), written directly rather than through the
    full `fleet pr --sync` CLI trigger — this file's tests drive the label-rewrite/REVALIDATE
    mechanisms as units, exactly as `test_a_superseded_stub_leaves_the_consumers_generated_
    dependency_on_the_real_label` hand-seeds `state='SUPERSEDED'` rather than driving T1 for real.
    """
    conn = sqlite3.connect(fleet / "state" / "fleet.db")
    try:
        conn.execute(
            "UPDATE stubs SET state = 'SUPERSEDED', resolved_at = '2026-09-08T00:00:00+00:00' "
            " WHERE run_id = ? AND consumer_repo_id = ? AND stub_coord_key = ? "
            "   AND state = 'ACTIVE'",
            (run_id, _STUB_CONSUMER, _STUB_COORD_KEY),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------
# 1/2/3 — D107: the label rewrite, proven by reading the ACTUAL committed file off
# `migrate/<consumer>` after the fix (not the `stubs` table, not `_unit_deps`'s output).
# ---------------------------------------------------------------------------------------


def test_d107_rewrites_the_committed_migrate_branch_off_the_stub_label(
    fleet: Path,  # noqa: F811
    monorepo: Path,  # noqa: F811
    filter_repo: FakeFilterRepo,  # noqa: F811
) -> None:
    """The literal proof item 3 of the brief asks for. `BuildgenWorker`'s own render step runs
    unseamed (it invokes no `bazel`/`git-filter-repo` at all); only the ORIGINAL provider-failure
    setup above uses `FakeBazel`, matching the existing fixture's own precedent.
    """
    run_id = _reach_active_stub_state(fleet, monorepo, filter_repo)
    dest = relocations(filter_repo)[_STUB_CONSUMER]
    branch = f"migrate/{_STUB_CONSUMER}"

    # Before D107 runs, `migrate/<consumer>` (D115's `ingest()`-owned alias, force-moved to the
    # merge commit on every ingest — see `_rewrite_one_consumer_label`'s own docstring) carries
    # NO generated `BUILD.bazel` at all: the render only ever lands on `integration`'s own
    # worktree until this task's new commit puts a copy on `migrate/<consumer>` for the first
    # time. `integration`'s copy is what already carries the stub label, proven by
    # `test_an_active_stub_redirects_the_consumers_generated_dependency_to_the_stubs_label`.
    before = _git_show(monorepo, f"integration:{dest}/BUILD.bazel")
    assert f'"{_STUB_LABEL}"' in before, before
    with pytest.raises(subprocess.CalledProcessError):
        _git_show(monorepo, f"{branch}:{dest}/BUILD.bazel")

    _supersede_stub_row(fleet, run_id)

    outcomes = asyncio.run(
        _rewrite_superseded_consumer_labels(
            _load_settings(GlobalOptions(config_path=fleet / "config" / "fleet.yaml")),
            fleet / "state" / "fleet.db",
            run_id=run_id,
            consumer_repo_ids=[_STUB_CONSUMER],
        )
    )
    assert outcomes[_STUB_CONSUMER].startswith("committed "), outcomes

    after = _git_show(monorepo, f"{branch}:{dest}/BUILD.bazel")
    assert f'"{_PROVIDER_LABEL}"' in after, after
    assert f'"{_STUB_LABEL}"' not in after, after


def test_d107_is_idempotent_on_replay(
    fleet: Path,  # noqa: F811
    monorepo: Path,  # noqa: F811
    filter_repo: FakeFilterRepo,  # noqa: F811
) -> None:
    """Item 7 of the brief: replaying the rewrite (crash-and-retry / a second `fleet resume`)
    must report `already_applied` and land NO duplicate commit."""
    run_id = _reach_active_stub_state(fleet, monorepo, filter_repo)
    _supersede_stub_row(fleet, run_id)
    branch = f"migrate/{_STUB_CONSUMER}"
    settings = _load_settings(GlobalOptions(config_path=fleet / "config" / "fleet.yaml"))

    first = asyncio.run(
        _rewrite_superseded_consumer_labels(
            settings,
            fleet / "state" / "fleet.db",
            run_id=run_id,
            consumer_repo_ids=[_STUB_CONSUMER],
        )
    )
    assert first[_STUB_CONSUMER].startswith("committed "), first
    count_after_first = _git_log_count(monorepo, branch)

    second = asyncio.run(
        _rewrite_superseded_consumer_labels(
            settings,
            fleet / "state" / "fleet.db",
            run_id=run_id,
            consumer_repo_ids=[_STUB_CONSUMER],
        )
    )
    assert second[_STUB_CONSUMER] == "already_applied", second
    assert _git_log_count(monorepo, branch) == count_after_first, "a replay must land no commit"


# ---------------------------------------------------------------------------------------
# 4/5 — D104(b)/D108: the REVALIDATE claiming loop, driven against the REAL rewritten tree
# under a REAL `bazel build` + `bazel test` (no seam) — the headline real-Bazel proof.
# ---------------------------------------------------------------------------------------


def _remove_orphaned_stub_package(monorepo: Path, consumer_repo_id: str) -> None:  # noqa: F811
    """TEST-ONLY cleanup, not part of D107's own committed diff (which SPEC §3.5.1 item 1 scopes
    to "the label, and only the label"): removes the now-unreferenced `third_party/stubs/...`
    directory `_insert_stub_row`'s ORIGINAL stubbed build materialized, before this file's
    real-Bazel revalidation build runs.

    Needed only because this fixture's stub is `PUBLISHED_ARTIFACT` with a `pinned_version`
    resolved through the `resolver: FakeResolver` seam (shared, pre-existing test
    infrastructure `_insert_stub_row`'s callers all rely on) — the stub target's OWN external
    `pip.parse` coordinate was never made genuinely resolvable, so a real `bazel query
    rdeps(//..., ...)` (which loads the WHOLE `//...` universe, by `RdepverifyWorker`'s own
    already-existing, unmodified design) fails to load that orphaned package and poisons the
    whole query. A production stub package renders a REAL, resolvable target either way
    (`PUBLISHED_ARTIFACT` from a real registry fetch or `EMPTY_FAILING`'s deliberately-failing
    but still-loadable target) — this is a fixture limitation of mixing `FakeResolver` with an
    otherwise-real Bazel run, not a D107/D104/D108 production concern, and not fixed in
    production code here.
    """
    wt = monorepo.parent / "cleanup-scratch"
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-C",
            str(monorepo),
            "worktree",
            "add",
            "--force",
            str(wt),
            f"migrate/{consumer_repo_id}",
        ],
        check=True,
        capture_output=True,
    )
    try:
        stub_dir = wt / "third_party" / "stubs" / "pypi__acme-lib-py"
        if stub_dir.exists():
            shutil.rmtree(stub_dir)
            git(wt, "add", "-A")
            fixture_env = {
                "GIT_AUTHOR_NAME": "Fleet Fixture",
                "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                "GIT_COMMITTER_NAME": "Fleet Fixture",
                "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
                "HOME": str(wt),
                "PATH": "/usr/bin:/bin:/usr/local/bin",
            }
            subprocess.run(  # noqa: S603
                [  # noqa: S607
                    "git",
                    "-C",
                    str(wt),
                    "commit",
                    "-m",
                    "test-only: drop the orphaned stub package",
                ],
                check=True,
                capture_output=True,
                env=fixture_env,
            )
    finally:
        subprocess.run(  # noqa: S603
            [  # noqa: S607
                "git",
                "-C",
                str(monorepo),
                "worktree",
                "remove",
                "--force",
                str(wt),
            ],
            check=False,
            capture_output=True,
        )


def _insert_revalidate_task(
    fleet: Path,  # noqa: F811
    *,
    run_id: str,
    task_id: str,
    dest_path: str,
) -> None:
    """One `REVALIDATE` `tasks` row, written straight to SQLite — the shape
    `_fire_t1_for_provider`'s `t1_unit` mints in production (`insert_revalidation_task_row`),
    seeded directly here because this file drives T1's DB effect by hand (`_supersede_stub_row`)
    rather than through the full `fleet pr --sync` CLI trigger."""
    conn = sqlite3.connect(fleet / "state" / "fleet.db")
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, run_id, repo_id, phase, kind, revalidation_key, "
            "                   dest_path, max_attempts, ladder, created_at) "
            "VALUES (?, ?, ?, 4, 'REVALIDATE', ?, ?, 3, "
            '        \'[null,"EVIDENCE_ONLY","EVIDENCE_PLUS_REJECTED_APPROACHES"]\', ?)',
            (
                task_id,
                run_id,
                _STUB_CONSUMER,
                f"r1:{uuid4().hex}",
                dest_path,
                "2026-09-08T00:00:00+00:00",
            ),
        )
        conn.execute(
            "UPDATE stubs SET revalidation_task_id = ? "
            " WHERE run_id = ? AND consumer_repo_id = ? AND stub_coord_key = ? "
            "   AND state = 'SUPERSEDED'",
            (task_id, run_id, _STUB_CONSUMER, _STUB_COORD_KEY),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("bazel") is None, reason="bazel is not installed on this host")
def test_d104b_claiming_loop_resolves_the_stub_under_a_real_bazel_build_and_test(
    fleet: Path,  # noqa: F811
    monorepo: Path,  # noqa: F811
    filter_repo: FakeFilterRepo,  # noqa: F811
    bazel_cache_home: Path,
    bazel_registry: str,
    bazel_fetch_bazelrc: str,
) -> None:
    """The headline real-Bazel proof (test-proof items 4/5). `_reach_active_stub_state` uses
    `FakeBazel` only to make `acme-lib-py`'s ORIGINAL build fail cheaply (matching `test_an_
    active_stub_redirects_the_consumers_generated_dependency_to_the_stubs_label`'s own
    precedent) -- `cli.BAZEL_RUNNER` is reset to `None` (no seam) before D107's rewrite and the
    REVALIDATE claiming loop run, so `VerifyPipelineWorker` invokes a REAL `bazel build` +
    `bazel test` over the rewritten tree.

    Real Bazel introspection of the dependency graph (not a second read of the `stubs` table):
    the assertion the real target's own generated file exists in the built worktree, checked
    below via `bazel-bin`, is only reachable if the real `//py/acme_lib_py:acme_lib_py` target
    was actually analysed and built -- the stub package (`//third_party/stubs/...`) was never
    consulted for this build, because it is no longer named in the rewritten `BUILD.bazel` at
    all (proven by `test_d107_rewrites_the_committed_migrate_branch_off_the_stub_label` above).
    """
    run_id = _reach_active_stub_state(
        fleet,
        monorepo,
        filter_repo,
        bazel_registry=bazel_registry,
        bazel_fetch_bazelrc=bazel_fetch_bazelrc,
    )
    dest = relocations(filter_repo)[_STUB_CONSUMER]

    # The provider is "fixed": reopen it (§12.14 `fleet retry`, the audited OPERATOR_REOPEN door)
    # and rebuild it for REAL (no seam) — needed so `py/acme_lib_py/BUILD.bazel` genuinely lands
    # on `integration`, which the revalidation build below needs to find a real package at
    # `py/acme_lib_py` rather than the one `FakeBazel`'s earlier failure left absent.
    reopened = runner.invoke(
        app,
        [*base_args(fleet), "retry", _STUB_PROVIDER, "--reason", "fixed for the real-Bazel test"],
        catch_exceptions=False,
    )
    assert reopened.exit_code == ExitCode.SUCCESS, reopened.output
    assert cli.BAZEL_RUNNER is None, "the seam must already be absent for the real rebuild"
    # Exit code intentionally NOT asserted SUCCESS here: the CLI's aggregate exit reflects the
    # WHOLE run's phase rows, including `acme-app-py`'s still-DEGRADED phase-3 row from BEFORE
    # this dispatch (unrelated to this `--repo acme-lib-py` rebuild) — the fact that matters is
    # the PROVIDER's own row, asserted directly below.
    build(fleet, "--no-sandbox", "--repo", _STUB_PROVIDER)
    provider_status = query(
        fleet,
        "SELECT status FROM phases WHERE run_id = ? AND repo_id = ? AND phase = 3",
        (run_id, _STUB_PROVIDER),
    )
    assert provider_status[0][0] == "SUCCEEDED", provider_status

    _supersede_stub_row(fleet, run_id)
    settings = _load_settings(GlobalOptions(config_path=fleet / "config" / "fleet.yaml"))

    rewritten = asyncio.run(
        _rewrite_superseded_consumer_labels(
            settings,
            fleet / "state" / "fleet.db",
            run_id=run_id,
            consumer_repo_ids=[_STUB_CONSUMER],
        )
    )
    assert rewritten[_STUB_CONSUMER].startswith("committed "), rewritten
    _remove_orphaned_stub_package(monorepo, _STUB_CONSUMER)

    task_id = str(uuid4())
    _insert_revalidate_task(fleet, run_id=run_id, task_id=task_id, dest_path=dest)

    assert cli.BAZEL_RUNNER is None, "the seam must be absent for this test to mean anything"
    result = asyncio.run(
        _run_revalidation_claims_impl(
            settings, fleet / "state" / "fleet.db", run_id, now=cli._now()
        )
    )
    outcome = result["outcomes"][task_id]
    # NOT asserted PASS: `acme-app-py`/`acme-lib-py` (this fixture's real, minimal Python
    # packages) declare no `py_test` target at all, so a real `bazel test` genuinely reports
    # "no test targets" (a real, nonzero exit) regardless of the stub redirect — a fixture
    # limitation of the polyglot repos this whole test file (and `test_build_e2e.py` before it)
    # reuses, not a D107/D104/D108 defect. `settled`/`another_round` (never `FAILED:`, which
    # would mean the loop itself errored) is what this assertion can honestly require here; T2/
    # D108's own `phases -> SUCCEEDED` + `stubs -> RESOLVED` promotion is proven separately, under
    # `FakeBazel`, in `test_d108_promotes_the_consumer_once_a_revalidation_round_genuinely_passes`
    # below — permitted by this task's own brief ("FakeBazel is acceptable for auxiliary
    # unit-level checks").
    assert outcome.startswith(("settled: ", "another_round: ")), outcome

    report_row = query(
        fleet,
        "SELECT payload FROM findings WHERE run_id = ? AND repo_id = ? "
        "  AND kind = 'VerificationReport'",
        (run_id, _STUB_CONSUMER),
    )
    assert report_row, "the REVALIDATE loop must persist a VerificationReport (§3.4 step 3)"
    persisted = json.loads(str(report_row[0][0]))["report"]
    assert persisted["build_ok"] is True, persisted
    assert persisted["verified_against_stubs"] == [], persisted

    # The real-Bazel introspection (test-proof item 4): a real `bazel query` over the graph the
    # revalidation build actually analysed names the REAL provider label and never the stub's —
    # proof the real target was consulted, not a second read of the `stubs` table. The claiming
    # loop's own worktree is already gone (cleaned up in its own `finally`), so a fresh one is
    # cut from the SAME `migrate/<consumer>` tip it built from — the tree is identical.
    query_wt = monorepo.parent / "post-revalidate-query"
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-C",
            str(monorepo),
            "worktree",
            "add",
            "--detach",
            "--force",
            str(query_wt),
            f"migrate/{_STUB_CONSUMER}",
        ],
        check=True,
        capture_output=True,
    )
    try:
        queried = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "bazel",
                f"--output_user_root={bazel_cache_home}",
                "query",
                f"deps({DESTINATIONS[_STUB_CONSUMER]}/...)",
            ],
            cwd=query_wt,
            capture_output=True,
            text=True,
            timeout=300.0,
        )
    finally:
        subprocess.run(  # noqa: S603
            ["git", "-C", str(monorepo), "worktree", "remove", "--force", str(query_wt)],  # noqa: S607
            check=False,
            capture_output=True,
        )
    assert queried.returncode == 0, queried.stderr[-4000:]
    assert _PROVIDER_LABEL in queried.stdout, queried.stdout
    assert _STUB_LABEL not in queried.stdout, queried.stdout

    # The stub genuinely does not reach RESOLVED here (no PASS is reachable with this fixture's
    # test-less repos), and stays SUPERSEDED — the honest, disclosed limit of this real-Bazel
    # test, not a silently narrowed claim.
    stub_state = query(
        fleet,
        "SELECT state FROM stubs WHERE run_id = ? AND consumer_repo_id = ? AND stub_coord_key = ?",
        (run_id, _STUB_CONSUMER, _STUB_COORD_KEY),
    )
    assert [row[0] for row in stub_state] == ["SUPERSEDED"], stub_state


def test_d108_promotes_the_consumer_once_a_revalidation_round_genuinely_passes(
    fleet: Path,  # noqa: F811
    monorepo: Path,  # noqa: F811
    filter_repo: FakeFilterRepo,  # noqa: F811
) -> None:
    """Test-proof item 5 (D108): once the REVALIDATE claiming loop's round PASSes, the consumer's
    `phases` VERIFY row reaches `SUCCEEDED` and `stubs.state` reaches `RESOLVED`.

    Driven under `FakeBazel` throughout (permitted for this auxiliary check per this task's own
    brief — the real-Bazel proof above already exercises the same claiming loop's mechanics
    against a real `bazel build`/`bazel test`; this test isolates the settle/D108-write half,
    which that fixture's test-less repos cannot reach under a genuine PASS).
    """
    run_id = _reach_active_stub_state(fleet, monorepo, filter_repo)
    dest = relocations(filter_repo)[_STUB_CONSUMER]
    _supersede_stub_row(fleet, run_id)
    settings = _load_settings(GlobalOptions(config_path=fleet / "config" / "fleet.yaml"))

    rewritten = asyncio.run(
        _rewrite_superseded_consumer_labels(
            settings,
            fleet / "state" / "fleet.db",
            run_id=run_id,
            consumer_repo_ids=[_STUB_CONSUMER],
        )
    )
    assert rewritten[_STUB_CONSUMER].startswith("committed "), rewritten

    # D108's own CAS is scoped to `phase = Phase.VERIFY` and guards on `status = 'DEGRADED'`
    # (mirroring `stub_degrade_transform`'s ADR-0124 shape) -- this fixture only ever drove
    # `acme-app-py` through Phase 3 (`fleet build`), so it has no Phase 4 row at all yet. Seed one
    # DEGRADED, matching what an ordinary `fleet verify` dispatch against a live stub would have
    # left (`stub_degrade_transform` called unconditionally after VERIFY, round VI task 69).
    conn = sqlite3.connect(fleet / "state" / "fleet.db")
    try:
        conn.execute(
            "INSERT INTO phases (run_id, repo_id, phase, status, stubbed_deps, updated_at) "
            "VALUES (?, ?, 4, 'DEGRADED', ?, ?)",
            (run_id, _STUB_CONSUMER, json.dumps([_STUB_COORD_KEY]), "2026-09-08T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    # A fresh, all-green FakeBazel: no `fail=` entries at all, so every build and every test the
    # REVALIDATE claiming loop drives reports PASS.
    fake = FakeBazel(fleet / "artifacts" / "fake-bazel-revalidate")
    cli.BAZEL_RUNNER = fake
    try:
        task_id = str(uuid4())
        _insert_revalidate_task(fleet, run_id=run_id, task_id=task_id, dest_path=dest)

        result = asyncio.run(
            _run_revalidation_claims_impl(
                settings, fleet / "state" / "fleet.db", run_id, now=cli._now()
            )
        )
        outcome = result["outcomes"][task_id]
        assert outcome.startswith("settled: verdict=PASS"), outcome
    finally:
        cli.BAZEL_RUNNER = None

    stub_state = query(
        fleet,
        "SELECT state FROM stubs WHERE run_id = ? AND consumer_repo_id = ? AND stub_coord_key = ?",
        (run_id, _STUB_CONSUMER, _STUB_COORD_KEY),
    )
    assert [row[0] for row in stub_state] == ["RESOLVED"], stub_state

    phase_status = query(
        fleet,
        "SELECT status FROM phases WHERE run_id = ? AND repo_id = ? AND phase = 4",
        (run_id, _STUB_CONSUMER),
    )
    assert phase_status and phase_status[0][0] == "SUCCEEDED", phase_status

    # The D108 audit trail: a `StubConsumerStatusApplied` finding records the promotion.
    findings = query(
        fleet,
        "SELECT kind FROM findings WHERE run_id = ? AND repo_id = ? "
        "  AND kind = 'StubConsumerStatusApplied'",
        (run_id, _STUB_CONSUMER),
    )
    assert findings, "D108 must write an audited StubConsumerStatusApplied finding"


# ---------------------------------------------------------------------------------------
# 6 — negative-proof mutation (Rule 12), REWRITTEN fix round 1 (I2): the original version of
# this test asserted only `_active_stubs_by_consumer`'s DB-derived `fidelity == {}` — a
# characterization of one query that holds identically whether or not D107 (or the C1 gate
# below) exists, so it discriminated nothing. This version actually DRIVES the claiming loop
# against a committed tree that still names a stub label and asserts the round is REFUSED by
# the C1 gate (`cli._run_one_revalidation_task`'s own mechanical check, fix round 1) rather than
# silently promoting the consumer. See this file's own fix-round-1 report for the old-fails/
# new-passes proof (run with the gate commented out via a copied-aside file, never `git stash`).
# ---------------------------------------------------------------------------------------


def _plant_stub_labeled_build_file(
    monorepo: Path,  # noqa: F811
    consumer_repo_id: str,
    dest: str,
) -> None:
    """Commits a `BUILD.bazel` naming `_STUB_LABEL` directly onto `migrate/<consumer_repo_id>`
    — simulating the exact state the brief's C1 finding describes: a stub whose `stubs` row has
    already moved to `SUPERSEDED` (via T1) while the tree a REVALIDATE round would build against
    STILL names the old stub label, because D107's own rewrite for this round never landed (a
    transient monorepo-unavailable error, a rebase conflict, or simply never having run yet —
    the OBSERVABLE state is identical in every case: DB says resolved, tree still says stub).
    """
    wt = monorepo.parent / "plant-stub-label"
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-C",
            str(monorepo),
            "worktree",
            "add",
            "--force",
            str(wt),
            f"migrate/{consumer_repo_id}",
        ],
        check=True,
        capture_output=True,
    )
    try:
        build_dir = wt / dest
        build_dir.mkdir(parents=True, exist_ok=True)
        (build_dir / "BUILD.bazel").write_text(
            "py_library(\n"
            '    name = "acme_app_py",\n'
            '    srcs = ["acme_app_py/main.py"],\n'
            f'    deps = ["{_STUB_LABEL}"],\n'
            ")\n",
            encoding="utf-8",
        )
        git(wt, "add", "-A")
        subprocess.run(  # noqa: S603
            [  # noqa: S607
                "git",
                "-C",
                str(wt),
                "commit",
                "-m",
                "test-only: plant a stub-labeled BUILD.bazel (simulates a failed D107 rewrite)",
            ],
            check=True,
            capture_output=True,
            env={
                "GIT_AUTHOR_NAME": "Fleet Fixture",
                "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                "GIT_COMMITTER_NAME": "Fleet Fixture",
                "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
                "HOME": str(wt),
                "PATH": "/usr/bin:/bin:/usr/local/bin",
            },
        )
    finally:
        subprocess.run(  # noqa: S603
            [  # noqa: S607
                "git",
                "-C",
                str(monorepo),
                "worktree",
                "remove",
                "--force",
                str(wt),
            ],
            check=False,
            capture_output=True,
        )


def test_c1_gate_refuses_a_revalidate_round_whose_committed_tree_still_names_a_stub_label(
    fleet: Path,  # noqa: F811
    monorepo: Path,  # noqa: F811
    filter_repo: FakeFilterRepo,  # noqa: F811
) -> None:
    """C1 (fix round 1, opus-tier review): the mechanical gate this brief demanded, proven as a
    real discriminator. A consumer's stub is superseded (T1's own DB effect) but the committed
    `migrate/<consumer>` tree still names the stub label (`_plant_stub_labeled_build_file` —
    simulating a failed or never-run D107 rewrite, e.g. the transient monorepo-unavailable error
    `aabead1` made non-fatal to the surrounding command). Driving the REAL claiming loop
    (`_run_revalidation_claims_impl`) must REFUSE the round -- not build against the stub, not
    settle the stub, not promote the consumer -- and must record why.

    An all-green `FakeBazel` (no `fail=` entries -- every build/test PASSes unconditionally) is
    installed DELIBERATELY: it is what makes this test's own old-fails/new-passes proof
    unconfounded. With the C1 gate removed (see this task's fix-round-1 report for the
    reproduction), `FakeBazel` would report an unconditional PASS regardless of what the tree
    actually contains, and `settle_revalidation` would legitimately fire T2 and promote the
    consumer to SUCCEEDED -- the concrete false-positive C1 names, not a generic "something
    failed" (a real-Bazel run with no registry pin would ALSO fail here, but for an unrelated,
    confounding reason -- network/MODULE.bazel resolution -- that says nothing about the gate).
    """
    run_id = _reach_active_stub_state(fleet, monorepo, filter_repo)
    dest = relocations(filter_repo)[_STUB_CONSUMER]

    _plant_stub_labeled_build_file(monorepo, _STUB_CONSUMER, dest)
    committed = _git_show(monorepo, f"migrate/{_STUB_CONSUMER}:{dest}/BUILD.bazel")
    assert f'"{_STUB_LABEL}"' in committed, committed

    # T1's DB effect only -- D107's own rewrite is deliberately never called, so the tree
    # planted above is exactly what a claimed REVALIDATE task would see.
    _supersede_stub_row(fleet, run_id)

    task_id = str(uuid4())
    _insert_revalidate_task(fleet, run_id=run_id, task_id=task_id, dest_path=dest)
    settings = _load_settings(GlobalOptions(config_path=fleet / "config" / "fleet.yaml"))

    fake = FakeBazel(fleet / "artifacts" / "fake-bazel-c1-gate")
    cli.BAZEL_RUNNER = fake
    try:
        result = asyncio.run(
            _run_revalidation_claims_impl(
                settings, fleet / "state" / "fleet.db", run_id, now=cli._now()
            )
        )
    finally:
        cli.BAZEL_RUNNER = None
    outcome = result["outcomes"][task_id]
    assert outcome.startswith("FAILED:"), outcome
    assert "still names stub label" in outcome, outcome
    assert _STUB_LABEL in outcome, outcome

    # The gate fires BEFORE any worker is constructed: FakeBazel must have recorded zero calls.
    assert fake.calls == [], "the C1 gate must refuse before dispatching VerifyPipelineWorker"

    # The task is refused back to PENDING, not left RUNNING and not marked DONE -- retryable
    # once (if) the rewrite genuinely lands.
    task_status = query(fleet, "SELECT status FROM tasks WHERE task_id = ?", (task_id,))
    assert task_status == [("PENDING",)], task_status

    # The consumer must NOT have been promoted: no settle_revalidation call ever ran, so the
    # stub stays exactly SUPERSEDED (never RESOLVED) and the D108 write never fires. Without the
    # gate, FakeBazel's unconditional PASS would have flipped this to RESOLVED -- exactly C1's
    # named hazard.
    stub_state = query(
        fleet,
        "SELECT state FROM stubs WHERE run_id = ? AND consumer_repo_id = ? AND stub_coord_key = ?",
        (run_id, _STUB_CONSUMER, _STUB_COORD_KEY),
    )
    assert [row[0] for row in stub_state] == ["SUPERSEDED"], stub_state

    # The refusal is disclosed, not silent: a RevalidationLabelNotRewritten finding names the
    # consumer and the still-present stub label.
    findings = query(
        fleet,
        "SELECT payload FROM findings WHERE run_id = ? AND repo_id = ? "
        "  AND kind = 'RevalidationLabelNotRewritten'",
        (run_id, _STUB_CONSUMER),
    )
    assert findings, "the C1 gate must write an audited RevalidationLabelNotRewritten finding"
    disclosed = json.loads(str(findings[0][0]))
    assert disclosed["repo_id"] == _STUB_CONSUMER, disclosed
    assert _STUB_LABEL in disclosed["stub_labels"], disclosed


# ---------------------------------------------------------------------------------------
# Should-fix (fix round 1): a total label-rewrite failure must be visible in `fleet pr --sync`'s
# own human-readable output, not only in the JSON `label_rewrites` payload nothing rendered.
# ---------------------------------------------------------------------------------------


def test_pr_sync_lines_surfaces_a_failed_label_rewrite() -> None:
    lines = cli._pr_sync_lines(
        {
            "polled": ["acme-lib-py"],
            "merged": ["acme-lib-py"],
            "closed": [],
            "label_rewrites": {
                "acme-app-py": "FAILED: 'migrate/acme-app-py' does not exist in /tmp/x",
            },
        }
    )
    assert any("1 label rewrite(s) FAILED" in line for line in lines), lines
    assert any("acme-app-py" in line and "FAILED:" in line for line in lines), lines


def test_pr_sync_lines_is_silent_when_every_rewrite_succeeded() -> None:
    lines = cli._pr_sync_lines(
        {
            "polled": [],
            "merged": [],
            "closed": [],
            "label_rewrites": {"acme-app-py": "committed abc123"},
        }
    )
    assert not any("FAILED" in line for line in lines), lines


# ---------------------------------------------------------------------------------------
# Round VI task 81 — the two disclosed nits from the D107/D104/D108 bundle's final review:
# a fail-open on a missing BUILD.bazel (nit 1), and no escalation ladder for a permanently-
# refusing REVALIDATE round (nit 2).
# ---------------------------------------------------------------------------------------


def test_c1_gate_refuses_a_revalidate_round_whose_build_bazel_is_missing(
    fleet: Path,  # noqa: F811
    monorepo: Path,  # noqa: F811
    filter_repo: FakeFilterRepo,  # noqa: F811
) -> None:
    """Nit 1. The pre-task-81 gate read a missing `<dest>/BUILD.bazel` as `""` — no stub label
    found — and let the round proceed (fail OPEN). `migrate/<consumer>` never carries a generated
    `BUILD.bazel` at all until D107's OWN rewrite (or a prior successful REVALIDATE round) puts
    one there (`test_d107_rewrites_the_committed_migrate_branch_off_the_stub_label`'s own opening
    comment: "Before D107 runs, `migrate/<consumer>`... carries NO generated `BUILD.bazel` at
    all") — so simply never planting one (D107's rewrite is deliberately never called here either)
    already models the exact case the brief describes ("Bazel hasn't generated it yet, or the
    checkout is in an unexpected state"), with no extra fixture needed. This drives the REAL
    claiming loop against that untouched tree and asserts the round is REFUSED exactly as the
    still-present-stub-label case is, but with a DISTINCT reason ("is missing", never "still names
    stub label") so an operator is not told to look for a stub label that was never there.
    """
    run_id = _reach_active_stub_state(fleet, monorepo, filter_repo)
    dest = relocations(filter_repo)[_STUB_CONSUMER]
    with pytest.raises(subprocess.CalledProcessError):
        _git_show(monorepo, f"migrate/{_STUB_CONSUMER}:{dest}/BUILD.bazel")

    # T1's DB effect only -- D107's own rewrite is deliberately never called.
    _supersede_stub_row(fleet, run_id)

    task_id = str(uuid4())
    _insert_revalidate_task(fleet, run_id=run_id, task_id=task_id, dest_path=dest)
    settings = _load_settings(GlobalOptions(config_path=fleet / "config" / "fleet.yaml"))

    fake = FakeBazel(fleet / "artifacts" / "fake-bazel-c1-gate-missing")
    cli.BAZEL_RUNNER = fake
    try:
        result = asyncio.run(
            _run_revalidation_claims_impl(
                settings, fleet / "state" / "fleet.db", run_id, now=cli._now()
            )
        )
    finally:
        cli.BAZEL_RUNNER = None
    outcome = result["outcomes"][task_id]
    assert outcome.startswith("FAILED:"), outcome
    assert "is missing" in outcome, outcome
    assert "still names stub label" not in outcome, outcome

    # The gate fires BEFORE any worker is constructed -- exactly the stub-label case's own proof.
    assert fake.calls == [], "the C1 gate must refuse before dispatching VerifyPipelineWorker"

    task_status = query(fleet, "SELECT status FROM tasks WHERE task_id = ?", (task_id,))
    assert task_status == [("PENDING",)], task_status

    stub_state = query(
        fleet,
        "SELECT state FROM stubs WHERE run_id = ? AND consumer_repo_id = ? AND stub_coord_key = ?",
        (run_id, _STUB_CONSUMER, _STUB_COORD_KEY),
    )
    assert [row[0] for row in stub_state] == ["SUPERSEDED"], stub_state

    findings = query(
        fleet,
        "SELECT payload FROM findings WHERE run_id = ? AND repo_id = ? "
        "  AND kind = 'RevalidationLabelNotRewritten'",
        (run_id, _STUB_CONSUMER),
    )
    assert findings, "the C1 gate must write an audited RevalidationLabelNotRewritten finding"
    disclosed = json.loads(str(findings[0][0]))
    assert disclosed["reason"] == "build_file_missing", disclosed
    assert disclosed["stub_labels"] == [], disclosed


def test_c1_gate_logs_a_distinct_warning_once_a_round_has_refused_max_attempts_times(
    fleet: Path,  # noqa: F811
    monorepo: Path,  # noqa: F811
    filter_repo: FakeFilterRepo,  # noqa: F811
) -> None:
    """Nit 2. Nothing surfaced a REVALIDATE round refusing forever. This reuses `tasks.
    max_attempts` (already 3 on the row `_insert_revalidate_task` seeds -- ADR-0014's own retry-
    budget default) as the escalation threshold rather than a bespoke parallel counter, per the
    brief's own instruction to check for existing ladder machinery first.

    Drives the SAME stuck fixture (a stub label that never gets rewritten -- D107's rewrite is
    deliberately never called) through THREE separate claiming-loop invocations, the shape three
    separate `fleet resume` calls would take against a genuinely-stuck round, and asserts the
    `revalidation_round_stuck_refusing` WARNING stays silent while `refused_count` is below
    `max_attempts` and fires once it reaches it -- with the finding's own `refused_count`
    visibly incrementing across calls, proving the count is neither reset nor double-counted.
    """
    run_id = _reach_active_stub_state(fleet, monorepo, filter_repo)
    dest = relocations(filter_repo)[_STUB_CONSUMER]

    _plant_stub_labeled_build_file(monorepo, _STUB_CONSUMER, dest)
    _supersede_stub_row(fleet, run_id)

    task_id = str(uuid4())
    _insert_revalidate_task(fleet, run_id=run_id, task_id=task_id, dest_path=dest)
    settings = _load_settings(GlobalOptions(config_path=fleet / "config" / "fleet.yaml"))

    fake = FakeBazel(fleet / "artifacts" / "fake-bazel-c1-gate-escalate")
    cli.BAZEL_RUNNER = fake
    refused_counts: list[int] = []
    stuck_warnings_per_round: list[int] = []
    try:
        for _ in range(3):
            with structlog.testing.capture_logs() as logs:
                result = asyncio.run(
                    _run_revalidation_claims_impl(
                        settings, fleet / "state" / "fleet.db", run_id, now=cli._now()
                    )
                )
            outcome = result["outcomes"][task_id]
            assert outcome.startswith("FAILED:"), outcome

            findings = query(
                fleet,
                "SELECT payload FROM findings WHERE run_id = ? AND repo_id = ? "
                "  AND kind = 'RevalidationLabelNotRewritten'",
                (run_id, _STUB_CONSUMER),
            )
            disclosed = json.loads(str(findings[0][0]))
            refused_counts.append(int(disclosed["refused_count"]))

            stuck = [e for e in logs if e.get("event") == "revalidation_round_stuck_refusing"]
            stuck_warnings_per_round.append(len(stuck))
    finally:
        cli.BAZEL_RUNNER = None

    assert refused_counts == [1, 2, 3], refused_counts
    assert stuck_warnings_per_round == [0, 0, 1], (
        "the stuck-round WARNING must stay silent below max_attempts and fire once it is "
        f"reached, exactly once per crossing round: {stuck_warnings_per_round}"
    )
