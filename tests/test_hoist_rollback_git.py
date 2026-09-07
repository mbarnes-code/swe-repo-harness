"""Round VI task 71 -- §12.31/D111 Leg D slice 2 (ADR-0122 Decisions 4/5): the git-mechanics
revert-series execution that runs AFTER `cli.unhoist_contract` (slice 1, round VI task 65)
consumes its `APPLIED` output.

Real git (a real monorepo checkout, real merge commits, a real disposable dry-check worktree) +
a real sqlite DB (`_pr_records`'s own `findings` table) + a fake `Forge` (the one seam this
function takes as an explicit, injected parameter per CLAUDE.md Guardrail 3 -- see
`execute_hoist_rollback`'s own docstring). Mirrors `tests/test_vcs.py`'s house style for git
mechanics and `tests/test_unhoist_rollback.py`'s house style for hand-seeded DB fixtures; this
file is deliberately standalone (round VI task 65's precedent: shipped with zero call sites
outside its own tests).
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fleet.cli import (
    PR_RECORD_KIND,
    HoistRollbackConflictError,
    HoistRollbackOutcome,
    OrderedRevertEntry,
    RollbackAnchorError,
    _ordered_revert_shas,
    execute_hoist_rollback,
)
from fleet.llm.roles import SPEC_ROLE_TIERS
from fleet.models.enums import PrState
from fleet.models.tasks import PullRequestDraft
from fleet.sandbox.worktree import WorktreeManager
from fleet.settings import FleetSettings
from fleet.state.db import connect_ro, initialize_database
from fleet.util.proc import run
from fleet.vcs.commits import FleetTrailers, revert_and_commit
from fleet.vcs.forge import PrStatus
from fleet.vcs.git import Git

RUN_ID = "33333333-3333-4333-8333-333333333333"
CONTRACT_ID = "proto:demo"
OWNER_REPO = "owner-repo"

# -- minimal config, mirroring tests/test_unhoist_rollback.py's own recipe exactly --------------
_ROLES_BLOCK = "".join(
    f"  {role.value}: {tier.value}\n" for role, tier in sorted(SPEC_ROLE_TIERS.items())
)
_MODELS_YAML = (
    "version: 2\nroles:\n" + _ROLES_BLOCK + "default_profile: default\nprofiles:\n  default:\n"
    "    HEAVY:\n"
    "      - { backend: anthropic, model_id: claude-opus-5, effort: high,\n"
    "          api_key_env: ANTHROPIC_API_KEY,\n"
    "          price: { in_per_mtok: 5.0, out_per_mtok: 25.0 } }\n"
    "    WORKHORSE:\n"
    "      - { backend: anthropic, model_id: claude-sonnet-5, effort: high,\n"
    "          api_key_env: ANTHROPIC_API_KEY,\n"
    "          price: { in_per_mtok: 3.0, out_per_mtok: 15.0 } }\n"
    "    CHEAP:\n"
    "      - { backend: anthropic, model_id: claude-haiku-4-5,\n"
    "          api_key_env: ANTHROPIC_API_KEY,\n"
    "          price: { in_per_mtok: 1.0, out_per_mtok: 5.0 } }\n"
)
_REPOS_YAML = (
    "version: 1\ndefaults:\n  ref: main\nrepos:\n"
    "  - name: acme-commons\n    url: https://github.com/acme/acme-commons\n"
)


def _fleet_yaml(monorepo_path: str, work_dir: str) -> str:
    return (
        f"run:\n  monorepo_path: {monorepo_path}\n  monorepo_branch: integration\n"
        f"  work_dir: {work_dir}\n"
        "preflight:\n  min_free_bytes: 1048576\n"
        "concurrency:\n  docker: 1\n"
        "verify:\n  container_memory: 64m\n"
        "budgets:\n  max_rss_mb: 512\n"
    )


def _settings(tmp_path: Path) -> FleetSettings:
    """`settings.root == tmp_path`; `monorepo_path`/`work_dir` both resolve INSIDE it, so a real
    git repo and a real disposable-worktree parent directory can both live under one `tmp_path`."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "fleet.yaml").write_text(_fleet_yaml("monorepo", "work"), encoding="utf-8")
    (config_dir / "models.yaml").write_text(_MODELS_YAML, encoding="utf-8")
    (config_dir / "repos.yaml").write_text(_REPOS_YAML, encoding="utf-8")
    (config_dir / "rules").mkdir(parents=True, exist_ok=True)
    return FleetSettings.load(config_dir)


# -- a real monorepo git repo, on `integration`, exactly the checkout `_monorepo_checkout` reads -
async def _sh(cwd: Path, *args: str) -> None:
    result = await run(["git", *args], cwd=cwd, timeout_s=60)
    assert result.exit_code == 0, (
        f"git {' '.join(args)} -> {result.exit_code}: {result.stderr_tail}"
    )


def _write(path: Path, name: str, text: str) -> None:
    target = path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


async def _write_and_commit(path: Path, name: str, text: str, subject: str) -> None:
    _write(path, name, text)
    await _sh(path, "add", "-A")
    await _sh(path, "commit", "-m", subject)


async def _init_monorepo(tmp_path: Path) -> Path:
    path = tmp_path / "monorepo"
    path.mkdir(parents=True, exist_ok=True)
    await _sh(path, "init", "-q", "--initial-branch=main", ".")
    await _sh(path, "config", "user.email", "fleet@example.invalid")
    await _sh(path, "config", "user.name", "Fleet Test")
    await _write_and_commit(path, "root.txt", "root\n", "initial")
    await _sh(path, "checkout", "-q", "-b", "integration")
    return path


async def _merge_feature(
    monorepo: Path, *, file_name: str, text: str, subject: str, feature_branch: str
) -> str:
    """A real two-parent merge commit onto `integration`, exactly the shape ADR-0122's revert
    series targets (`git revert -m 1`)."""
    await _sh(monorepo, "checkout", "-q", "-b", feature_branch)
    await _write_and_commit(monorepo, file_name, text, "feature commit")
    await _sh(monorepo, "checkout", "-q", "integration")
    await _sh(monorepo, "merge", "-q", "--no-ff", "-m", subject, feature_branch)
    git = Git(monorepo, timeout_s=60)
    return await git.rev_parse("integration")


# -- a real sqlite DB, seeded exactly as `_pr_records` reads it ---------------------------------
@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "fleet.db"
    await initialize_database(path)
    return path


def _seed_run(db_path: Path) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO runs (run_id, started_at, config_sha256, config_digests, "
            "                  harness_version) VALUES (?, ?, ?, '{}', ?)",
            (RUN_ID, "2026-09-07T00:00:00+00:00", "a" * 64, "0.1.0"),
        )
        for repo_id in (OWNER_REPO, "blast-a", "blast-b", "blast-unmerged"):
            conn.execute(
                "INSERT INTO repos (repo_id, name, url, updated_at) VALUES (?, ?, ?, ?)",
                (
                    repo_id,
                    repo_id,
                    f"https://example.invalid/{repo_id}",
                    "2026-09-07T00:00:00+00:00",
                ),
            )
    finally:
        conn.close()


def _seed_pr(
    db_path: Path,
    *,
    repo_id: str,
    url: str,
    state: PrState,
    contract_id: str | None = None,
) -> None:
    draft = PullRequestDraft(
        run_id=uuid.UUID(RUN_ID),
        repo_id=repo_id,
        contract_id=contract_id,
        wave_index=0,
        branch=f"migrate/{repo_id}",
        title=f"[fleet] migrate {repo_id}",
        body="body",
        source_url=f"https://example.invalid/{repo_id}",
        source_sha="a" * 40,
        state=state,
        url=url,
    )
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload, "
            "                      created_at) VALUES (?, ?, ?, 'info', ?, ?, ?)",
            (
                RUN_ID,
                repo_id,
                PR_RECORD_KIND,
                f"pr:{repo_id}",
                draft.model_dump_json(),
                "2026-09-07T00:00:00+00:00",
            ),
        )
    finally:
        conn.close()


class FakeForge:
    """The one seam `execute_hoist_rollback`/`_ordered_revert_shas` take as an explicit,
    injected parameter (CLAUDE.md Guardrail 3) -- a real network/forge call has no place in a
    unit test of this function's own revert-series logic. Only `view()` is implemented: nothing
    here calls any other `Forge` method."""

    def __init__(self, by_url: Mapping[str, PrStatus]) -> None:
        self._by_url = dict(by_url)
        self.calls: list[str] = []

    async def view(self, url: str) -> PrStatus:
        self.calls.append(url)
        try:
            return self._by_url[url]
        except KeyError:
            raise AssertionError(f"FakeForge.view called with unscripted url {url!r}") from None


def _status(sha: str, merged_at: datetime) -> PrStatus:
    return PrStatus(url="", state=PrState.MERGED, merged_at=merged_at, merge_commit_sha=sha)


T0 = datetime(2026, 9, 1, tzinfo=UTC)


# --------------------------------------------------------------------------------------
# Proof 1: a genuine multi-parent-merge revert series -- the contract's own hoist plus two
# already-merged blast-set members, all reverted cleanly, real-pass commits carry the trailer,
# and `contract_rollback_shas_in_range` round-trips them (Rule 12's primary proof).
# --------------------------------------------------------------------------------------
async def test_execute_hoist_rollback_commits_a_real_multi_merge_revert_series(
    tmp_path: Path, db_path: Path
) -> None:
    monorepo = await _init_monorepo(tmp_path)
    merge_contract = await _merge_feature(
        monorepo,
        file_name="contract.txt",
        text="hoisted\n",
        subject="merge contract hoist",
        feature_branch="feat-contract",
    )
    merge_a = await _merge_feature(
        monorepo,
        file_name="a.txt",
        text="consumer a\n",
        subject="merge blast-a",
        feature_branch="feat-a",
    )
    merge_b = await _merge_feature(
        monorepo,
        file_name="b.txt",
        text="consumer b\n",
        subject="merge blast-b",
        feature_branch="feat-b",
    )
    tip_before = await Git(monorepo, timeout_s=60).rev_parse("integration")

    _seed_run(db_path)
    _seed_pr(
        db_path,
        repo_id=OWNER_REPO,
        url="https://forge.invalid/owner/pull/1",
        state=PrState.MERGED,
        contract_id=CONTRACT_ID,
    )
    _seed_pr(db_path, repo_id="blast-a", url="https://forge.invalid/a/pull/1", state=PrState.MERGED)
    _seed_pr(db_path, repo_id="blast-b", url="https://forge.invalid/b/pull/1", state=PrState.MERGED)
    _seed_pr(
        db_path,
        repo_id="blast-unmerged",
        url="https://forge.invalid/u/pull/1",
        state=PrState.DRAFTED,
    )

    forge = FakeForge(
        {
            "https://forge.invalid/owner/pull/1": _status(merge_contract, T0),
            "https://forge.invalid/a/pull/1": _status(merge_a, T0 + timedelta(hours=1)),
            "https://forge.invalid/b/pull/1": _status(merge_b, T0 + timedelta(hours=2)),
        }
    )

    read_conn = await connect_ro(db_path)
    try:
        settings = _settings(tmp_path)
        outcome = await execute_hoist_rollback(
            read_conn,
            settings,
            run_id=RUN_ID,
            contract_id=CONTRACT_ID,
            blast_set=("blast-a", "blast-b", "blast-unmerged"),
            forge=forge,
        )
    finally:
        await read_conn.close()

    assert isinstance(outcome, HoistRollbackOutcome)
    # Reverse-chronological: blast-b (merged last) reverted first, contract's own hoist last.
    assert outcome.ordered_shas == (merge_b, merge_a, merge_contract)
    assert outcome.decision == "COMMITTED"
    assert outcome.already_reverted_shas == ()
    assert outcome.newly_reverted_shas == (merge_b, merge_a, merge_contract)

    git = Git(monorepo, timeout_s=60)
    tip_after = await git.rev_parse("integration")
    assert tip_after != tip_before, "three new revert commits must have landed"
    assert not (monorepo / "contract.txt").exists()
    assert not (monorepo / "a.txt").exists()
    assert not (monorepo / "b.txt").exists()
    # The original merge commits are still present, unmodified -- a REVERT, never a rewrite
    # (CLAUDE.md guardrail 4).
    for original in (merge_contract, merge_a, merge_b):
        assert await git.text(["log", "-1", "--format=%H", original]) == original

    from fleet.vcs.commits import contract_rollback_shas_in_range

    reverted = await contract_rollback_shas_in_range(
        git, pre_commit_sha=merge_contract, branch="integration", contract_id=CONTRACT_ID
    )
    assert reverted == frozenset({merge_contract, merge_a, merge_b})

    # No disposable dry-check worktree survives.
    work_dir = tmp_path / "work"
    leftovers = list(work_dir.glob(f"*unhoist-{CONTRACT_ID}*")) if work_dir.exists() else []
    assert leftovers == [], f"disposable worktree not cleaned up: {leftovers}"


# --------------------------------------------------------------------------------------
# Proof 2: a genuine dry-check conflict -- the REAL branch must stay untouched, the disposable
# worktree gone, and the settled outcome must report the refusal (not raise).
# --------------------------------------------------------------------------------------
async def test_execute_hoist_rollback_refuses_the_whole_series_on_a_genuine_dry_check_conflict(
    tmp_path: Path, db_path: Path
) -> None:
    monorepo = await _init_monorepo(tmp_path)
    # The contract's own hoist edits shared.txt.
    merge_contract = await _merge_feature(
        monorepo,
        file_name="shared.txt",
        text="hoisted\n",
        subject="merge contract hoist",
        feature_branch="feat-contract",
    )
    # blast-a's merge is unrelated (different file) -- its own revert stages cleanly.
    merge_a = await _merge_feature(
        monorepo,
        file_name="a.txt",
        text="consumer a\n",
        subject="merge blast-a",
        feature_branch="feat-a",
    )
    # History "moved on": a later, unrelated direct commit edits shared.txt again -- reverting
    # the contract's own hoist (second in revert order, since it merged first) must now conflict.
    await _write_and_commit(monorepo, "shared.txt", "hoisted\nmain-edit-after\n", "later edit")
    git = Git(monorepo, timeout_s=60)
    tip_before = await git.rev_parse("integration")

    _seed_run(db_path)
    _seed_pr(
        db_path,
        repo_id=OWNER_REPO,
        url="https://forge.invalid/owner/pull/1",
        state=PrState.MERGED,
        contract_id=CONTRACT_ID,
    )
    _seed_pr(db_path, repo_id="blast-a", url="https://forge.invalid/a/pull/1", state=PrState.MERGED)

    forge = FakeForge(
        {
            "https://forge.invalid/owner/pull/1": _status(merge_contract, T0),
            "https://forge.invalid/a/pull/1": _status(merge_a, T0 + timedelta(hours=1)),
        }
    )

    read_conn = await connect_ro(db_path)
    try:
        settings = _settings(tmp_path)
        outcome = await execute_hoist_rollback(
            read_conn,
            settings,
            run_id=RUN_ID,
            contract_id=CONTRACT_ID,
            blast_set=("blast-a",),
            forge=forge,
        )
    finally:
        await read_conn.close()

    assert outcome.decision == "REFUSED_CONFLICT"
    assert outcome.newly_reverted_shas == ()
    assert outcome.already_reverted_shas == ()
    assert outcome.ordered_shas == (merge_a, merge_contract)

    assert await git.rev_parse("integration") == tip_before, "the real branch must be untouched"
    assert await git.resolve("REVERT_HEAD") is None, "no dangling revert state on the REAL repo"

    work_dir = tmp_path / "work"
    leftovers = list(work_dir.glob(f"*unhoist-{CONTRACT_ID}*")) if work_dir.exists() else []
    assert leftovers == [], f"disposable worktree not cleaned up after a conflict: {leftovers}"


# --------------------------------------------------------------------------------------
# Proof 3: a genuine tip-moved-between-dry-check-and-real-pass race. Advance `integration` (via a
# `WorktreeManager.remove` patch -- the natural hook point between the dry-check and the CAS
# re-read) and assert the function detects it and aborts without partially committing.
# --------------------------------------------------------------------------------------
async def test_execute_hoist_rollback_detects_a_tip_moved_race_and_aborts(
    tmp_path: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monorepo = await _init_monorepo(tmp_path)
    merge_contract = await _merge_feature(
        monorepo,
        file_name="contract.txt",
        text="hoisted\n",
        subject="merge contract hoist",
        feature_branch="feat-contract",
    )
    merge_a = await _merge_feature(
        monorepo,
        file_name="a.txt",
        text="consumer a\n",
        subject="merge blast-a",
        feature_branch="feat-a",
    )
    git = Git(monorepo, timeout_s=60)
    tip_before_race = await git.rev_parse("integration")

    _seed_run(db_path)
    _seed_pr(
        db_path,
        repo_id=OWNER_REPO,
        url="https://forge.invalid/owner/pull/1",
        state=PrState.MERGED,
        contract_id=CONTRACT_ID,
    )
    _seed_pr(db_path, repo_id="blast-a", url="https://forge.invalid/a/pull/1", state=PrState.MERGED)

    forge = FakeForge(
        {
            "https://forge.invalid/owner/pull/1": _status(merge_contract, T0),
            "https://forge.invalid/a/pull/1": _status(merge_a, T0 + timedelta(hours=1)),
        }
    )

    real_remove = WorktreeManager.remove
    moved = {"done": False}

    async def _remove_and_advance_tip(
        self: WorktreeManager, target: object, **kwargs: object
    ) -> bool:
        # The natural hook point: this runs in the `finally` block right after the dry-check
        # loop, immediately before `execute_hoist_rollback` re-reads the REAL tip. A concurrent
        # writer landing a commit in exactly this window is the race Decision 4 point 3 exists
        # to catch.
        result = await real_remove(self, target, **kwargs)  # type: ignore[arg-type]
        if not moved["done"]:
            moved["done"] = True
            await _write_and_commit(
                monorepo, "concurrent.txt", "x\n", "a concurrent writer's commit"
            )
        return result

    monkeypatch.setattr(WorktreeManager, "remove", _remove_and_advance_tip)

    read_conn = await connect_ro(db_path)
    try:
        settings = _settings(tmp_path)
        outcome = await execute_hoist_rollback(
            read_conn,
            settings,
            run_id=RUN_ID,
            contract_id=CONTRACT_ID,
            blast_set=("blast-a",),
            forge=forge,
        )
    finally:
        await read_conn.close()

    assert outcome.decision == "RACE_ABORTED"
    assert outcome.newly_reverted_shas == ()
    assert outcome.already_reverted_shas == ()

    tip_after = await git.rev_parse("integration")
    assert tip_after != tip_before_race, "the concurrent commit really did move the tip"
    concurrent_sha = tip_after
    assert await git.text(["log", "-1", "--format=%H", concurrent_sha]) == concurrent_sha
    # Nothing from the rollback itself landed -- the tip moved by exactly the one concurrent
    # commit, not by that commit PLUS a partial revert series.
    assert (monorepo / "contract.txt").exists() and (monorepo / "a.txt").exists()


# --------------------------------------------------------------------------------------
# Proof 4: crash recovery. Land the FIRST real-pass commit directly (this is the "crash after one
# commit" the brief asks the test to control explicitly, not simulate via a real process kill),
# then re-invoke the function fresh and assert it reverts only the remaining sha, never
# re-reverting the first, using the trailer query as its only source of truth.
# --------------------------------------------------------------------------------------
async def test_execute_hoist_rollback_resumes_after_a_crash_without_re_reverting_the_first_sha(
    tmp_path: Path, db_path: Path
) -> None:
    monorepo = await _init_monorepo(tmp_path)
    merge_contract = await _merge_feature(
        monorepo,
        file_name="contract.txt",
        text="hoisted\n",
        subject="merge contract hoist",
        feature_branch="feat-contract",
    )
    merge_a = await _merge_feature(
        monorepo,
        file_name="a.txt",
        text="consumer a\n",
        subject="merge blast-a",
        feature_branch="feat-a",
    )
    git = Git(monorepo, timeout_s=60)

    _seed_run(db_path)
    _seed_pr(
        db_path,
        repo_id=OWNER_REPO,
        url="https://forge.invalid/owner/pull/1",
        state=PrState.MERGED,
        contract_id=CONTRACT_ID,
    )
    _seed_pr(db_path, repo_id="blast-a", url="https://forge.invalid/a/pull/1", state=PrState.MERGED)

    forge = FakeForge(
        {
            "https://forge.invalid/owner/pull/1": _status(merge_contract, T0),
            "https://forge.invalid/a/pull/1": _status(merge_a, T0 + timedelta(hours=1)),
        }
    )

    # "Crash after only the first real-pass commit lands" -- ordered series is (merge_a,
    # merge_contract) (blast-a merged later, reverted first). Land exactly that first commit
    # directly, using the SAME primitive and trailer shape `execute_hoist_rollback`'s real pass
    # uses, then abandon (no second commit, exactly a mid-series crash).
    crash_outcome = await revert_and_commit(
        git,
        sha=merge_a,
        subject=f"fleet: hoist rollback for {CONTRACT_ID} (revert {merge_a[:12]})",
        trailers=FleetTrailers(
            run_id=RUN_ID,
            repo_id=CONTRACT_ID,
            phase=3,
            task_id=CONTRACT_ID,
            attempt=1,
            patch_id="0" * 64,
            contract_rollback_id=CONTRACT_ID,
        ),
        mainline=1,
        body=f"This reverts commit {merge_a}.",
    )
    assert crash_outcome.conflicted is False
    tip_after_crash = await git.rev_parse("integration")
    assert not (monorepo / "a.txt").exists()
    assert (monorepo / "contract.txt").exists(), "only the FIRST sha was reverted before the crash"

    read_conn = await connect_ro(db_path)
    try:
        settings = _settings(tmp_path)
        outcome = await execute_hoist_rollback(
            read_conn,
            settings,
            run_id=RUN_ID,
            contract_id=CONTRACT_ID,
            blast_set=("blast-a",),
            forge=forge,
        )
    finally:
        await read_conn.close()

    assert outcome.decision == "COMMITTED"
    assert outcome.ordered_shas == (merge_a, merge_contract)
    assert outcome.already_reverted_shas == (merge_a,)
    assert outcome.newly_reverted_shas == (merge_contract,), (
        "must revert only the remaining sha; re-reverting merge_a would be a no-op patch at best "
        "and a real defect at worst (git would refuse the second revert of an already-reverted "
        "merge as a conflict)"
    )
    assert not (monorepo / "contract.txt").exists(), "the second, previously-uncommitted sha landed"

    tip_final = await git.rev_parse("integration")
    assert tip_final != tip_after_crash, "exactly one new commit must have landed (the resumed one)"
    parents = (await git.text(["log", "-1", "--format=%P", tip_final])).split()
    assert parents == [tip_after_crash], "the resumed commit's only parent is the crash point"


# --------------------------------------------------------------------------------------
# `_ordered_revert_shas` in isolation: the contract-record-lookup correction this task's own
# research verified (scanning for `draft.contract_id`, never a keyed `records.get(contract_id)`).
# --------------------------------------------------------------------------------------
async def test_ordered_revert_shas_finds_the_contracts_own_draft_by_scanning_not_by_key(
    db_path: Path,
) -> None:
    _seed_run(db_path)
    _seed_pr(
        db_path,
        repo_id=OWNER_REPO,
        url="https://forge.invalid/owner/pull/1",
        state=PrState.MERGED,
        contract_id=CONTRACT_ID,
    )
    _seed_pr(db_path, repo_id="blast-a", url="https://forge.invalid/a/pull/1", state=PrState.MERGED)

    forge = FakeForge(
        {
            "https://forge.invalid/owner/pull/1": _status("c" * 40, T0),
            "https://forge.invalid/a/pull/1": _status("a" * 40, T0 + timedelta(hours=1)),
        }
    )

    read_conn = await connect_ro(db_path)
    try:
        # A keyed lookup on `contract_id` (the WRONG mechanism this task's research ruled out)
        # would find nothing at `records[CONTRACT_ID]` -- `_pr_records` is keyed by `repo_id`
        # (`OWNER_REPO`), never by `contract_id`. This proves the scan is what actually works.
        entries = await _ordered_revert_shas(
            read_conn, RUN_ID, forge, contract_id=CONTRACT_ID, blast_set=("blast-a",)
        )
    finally:
        await read_conn.close()

    assert entries == (
        OrderedRevertEntry(
            repo_id="blast-a", merge_sha="a" * 40, merged_at=T0 + timedelta(hours=1)
        ),
        OrderedRevertEntry(repo_id=None, merge_sha="c" * 40, merged_at=T0),
    )


async def test_execute_hoist_rollback_raises_loud_when_the_contracts_own_pr_is_unresolvable(
    tmp_path: Path, db_path: Path
) -> None:
    """No contract-tagged PR draft at all (research-40's own measured gap: nothing in `src/fleet/`
    currently constructs one in production) -- but a non-empty blast set with a real MERGED PR --
    must raise loud (Rule 11) rather than silently pick a wrong anchor for Decision 5's resume
    query, per this function's own documented precondition."""
    monorepo = await _init_monorepo(tmp_path)
    merge_a = await _merge_feature(
        monorepo,
        file_name="a.txt",
        text="consumer a\n",
        subject="merge blast-a",
        feature_branch="feat-a",
    )
    _seed_run(db_path)
    _seed_pr(db_path, repo_id="blast-a", url="https://forge.invalid/a/pull/1", state=PrState.MERGED)
    forge = FakeForge({"https://forge.invalid/a/pull/1": _status(merge_a, T0)})

    read_conn = await connect_ro(db_path)
    try:
        settings = _settings(tmp_path)
        with pytest.raises(RollbackAnchorError):
            await execute_hoist_rollback(
                read_conn,
                settings,
                run_id=RUN_ID,
                contract_id=CONTRACT_ID,
                blast_set=("blast-a",),
                forge=forge,
            )
    finally:
        await read_conn.close()


async def test_execute_hoist_rollback_is_a_no_op_when_nothing_is_merged_yet(
    tmp_path: Path, db_path: Path
) -> None:
    """No MERGED PR anywhere (contract or blast set) -- nothing to revert, a settled `COMMITTED`
    with every tuple empty, never a crash on an absent anchor (the anchor-required path is only
    reached once there is something non-empty to anchor)."""
    _seed_run(db_path)
    _seed_pr(
        db_path, repo_id="blast-a", url="https://forge.invalid/a/pull/1", state=PrState.DRAFTED
    )
    forge = FakeForge({})

    read_conn = await connect_ro(db_path)
    try:
        settings = _settings(tmp_path)
        outcome = await execute_hoist_rollback(
            read_conn,
            settings,
            run_id=RUN_ID,
            contract_id=CONTRACT_ID,
            blast_set=("blast-a",),
            forge=forge,
        )
    finally:
        await read_conn.close()

    assert outcome == HoistRollbackOutcome(
        decision="COMMITTED",
        contract_id=CONTRACT_ID,
        ordered_shas=(),
        already_reverted_shas=(),
        newly_reverted_shas=(),
    )


def test_hoist_rollback_conflict_error_is_a_git_error() -> None:
    """Rooted at `GitError` (mirroring `RollbackAnchorError`/`RollbackIndeterminateError` in
    `vcs/commits.py`), so a caller catching the git-error family catches this too."""
    from fleet.vcs.git import GitError

    assert issubclass(HoistRollbackConflictError, GitError)
