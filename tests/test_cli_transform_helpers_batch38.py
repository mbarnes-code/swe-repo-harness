"""Round VIII, §15.1 item 3, Wave 7.4 batch 38 — mutation-proof `cli.py`'s remaining transform
helper functions (group G5): `_git_output`, `_nul_fields`, `_tracked_at`, `_transform_rules`,
`_rewrite_targets`, `_prepare_repo`, `_abandon_repo`, `_wave_repos`, `_import_specifiers`,
`_dest_paths`, `_ordering_pairs`.

Prior coverage, checked by grepping `tests/*.py` for each symbol before writing anything:

* `_git_output`, `_nul_fields`, `_wave_repos`, `_import_specifiers`, `_dest_paths`,
  `_ordering_pairs` — **zero** direct references anywhere (only reached transitively through
  `fleet transform`/`fleet status` end-to-end runs, or in one case not reached by any test file at
  all — `_import_specifiers`/`_ordering_pairs` have no e2e caller that asserts their own return
  value).
* `_tracked_at` — one docstring mention in `tests/test_transform_e2e.py`, no direct call.
* `_rewrite_targets` — one docstring mention in `tests/test_cli.py`, no direct call.
* `_transform_rules` — genuinely covered already: `tests/test_cli.py`'s
  `test_a_missing_rules_dir_is_refused_not_silently_zero_rules` and
  `test_a_present_but_empty_rules_dir_is_the_legitimate_zero_rules_fleet` cover both of its two
  branches (missing dir raises `ConfigFileError`; present-and-empty returns `()`) directly, and
  `tests/test_transform_e2e.py`'s rewrite tests exercise the real-rules-loaded path indirectly
  end to end. No new test added for it here; see the mutation-proof section below for the
  confirming check.
* `_prepare_repo` (32 refs) / `_abandon_repo` (12 refs) — heavily exercised already through
  `tests/test_transform_e2e.py` and direct calls in `tests/test_cli.py`
  (`test_a_timed_out_resolve_routes_to_abandon_not_a_branch_reset`,
  `test_abandon_repo_redacts_a_credential_in_detail_before_writing_last_error`, and siblings). Two
  concrete gaps were found and are closed here: `_prepare_repo`'s `dest_path is None` refusal
  (§3.3's `layout()` REFUSAL path) had no test anywhere, and `_abandon_repo`'s `phase`/`kind`
  parameterization — added specifically so Phase 3's git-preparation failures share this function
  (see its own docstring) — was never exercised with a non-default value.

Each test below calls the function DIRECTLY against a real (schema-fresh, file-backed) sqlite
database or a real git repository, following the same "call the private function against real
infrastructure" pattern `tests/test_contract_extraction_plumbing.py` (batch 35) and
`tests/test_cli_scan_worker_and_helpers.py` (batch 36) already established — never a full `fleet
transform` CLI invocation, because the branch each test targets is a property of the function
itself, not of anything an end-to-end run would need to prove afresh.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from fleet import ecosystems
from fleet.cli import (
    TransformStepUnavailableError,
    _abandon_repo,
    _dest_paths,
    _git_output,
    _import_specifiers,
    _nul_fields,
    _ordering_pairs,
    _prepare_repo,
    _rewrite_targets,
    _tracked_at,
    _wave_repos,
)
from fleet.models.enums import Phase
from fleet.rewrite.rules import RewriteRule
from fleet.state.db import SCHEMA_PATH, StateWriter
from fleet.vcs.git import Git, GitCommandError
from tests.test_cli import RUN_ID, FleetSettings, write_config


def _fresh_db(path: Path) -> Path:
    """A schema-fresh sqlite database with one seeded `runs` row, self-contained (no dependency
    on `tests/test_cli.py`'s `workspace` fixture, which needs a full `config/` bundle this file
    does not always need)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO runs (run_id, started_at, config_sha256, harness_version) "
            "VALUES (?, '2026-09-14T00:00:00Z', 'deadbeef', 'test')",
            (RUN_ID,),
        )
    finally:
        conn.close()
    return path


def _insert_repo(
    conn: sqlite3.Connection,
    repo_id: str,
    *,
    dest_path: str | None,
    ecosystems: str = "[]",
) -> None:
    conn.execute(
        "INSERT INTO repos (repo_id, name, url, dest_path, ecosystems, updated_at) "
        "VALUES (?, ?, ?, ?, ?, '2026-09-14T00:00:00Z')",
        (repo_id, repo_id, f"https://example.invalid/{repo_id}", dest_path, ecosystems),
    )


# =======================================================================================
# 1 — `_git_output`: the FULL stdout, read back off disk (not `Git.text`'s bounded tail)
# =======================================================================================


async def _init_repo(root: Path) -> Path:
    worktree = root / "repo"
    worktree.mkdir()
    git = Git(worktree)
    await git.exec(["init", "-q", "-b", "main"])
    return worktree


async def test_git_output_reads_the_complete_listing_off_disk(tmp_path: Path) -> None:
    """`Git.text` truncates to a bounded tail (§11.3) -- right for a diagnostic, wrong for a file
    list. This proves `_git_output` reads the FULL file `util.proc` streamed to disk: every one of
    many tracked files must be present, not merely the invocation succeeding.

    Mutation target: swapping the `result.stdout_path.read_text(...)` return for
    `result.stderr_tail` (or any bounded/partial read) would still return *something* on a
    successful call but not the exact byte-for-byte content this test pins.
    """
    worktree = await _init_repo(tmp_path)
    names = [f"file{i:03d}.txt" for i in range(50)]
    for name in names:
        (worktree / name).write_text("x\n")
    git = Git(worktree)
    await git.exec(["add", "--", *names])
    await git.commit("add many files")

    output = await _git_output(worktree, ["ls-tree", "-r", "-z", "--name-only", "HEAD"])

    fields = [f for f in output.split("\0") if f]
    assert sorted(fields) == sorted(names), (
        "every tracked file must appear in the full stdout, not a truncated subset"
    )


async def test_git_output_raises_git_command_error_on_a_failing_invocation(
    tmp_path: Path,
) -> None:
    """The failure branch: a non-zero exit raises `GitCommandError` carrying the argv, not a
    silently empty string or a swallowed exception (Rule 11)."""
    worktree = await _init_repo(tmp_path)

    with pytest.raises(GitCommandError) as raised:
        await _git_output(worktree, ["cat-file", "-p", "not-a-real-object"])

    assert "cat-file" in " ".join(raised.value.argv)


# =======================================================================================
# 2 — `_nul_fields`: filters the empty trailing field `-z` output always carries
# =======================================================================================


def test_nul_fields_drops_empty_fields_but_keeps_real_ones() -> None:
    """`git ... -z` output ends with a trailing NUL, so a naive `split("\\0")` always yields one
    trailing empty string; `_nul_fields` must drop exactly the empty ones and nothing else.

    Mutation target: a filter that drops falsy-but-meaningful values, or one that keeps the
    trailing empty field, would corrupt every caller's file count by one (`_tracked_at`) or
    corrupt `_changed_entries`'s (status, path, path) triple alignment.
    """
    assert _nul_fields("a\0b\0c\0") == ["a", "b", "c"]
    assert _nul_fields("") == []
    assert _nul_fields("\0\0") == []
    assert _nul_fields("only-one\0") == ["only-one"]


# =======================================================================================
# 3 — `_tracked_at`: the tree AT A REV, not the working tree
# =======================================================================================


async def test_tracked_at_reads_the_given_rev_not_the_current_worktree(tmp_path: Path) -> None:
    """§3.2 step 1's relocation plan is scoped to the PHASE ANCHOR commit, not whatever the
    worktree happens to hold right now. A file deleted after the anchor must still be reported at
    the anchor rev, and a file added after it must NOT be. Also proves the result is sorted.

    Mutation target: passing `"HEAD"` (or any fixed rev) instead of the `rev` parameter would
    make this test's two rev's answers identical; they must differ.
    """
    worktree = await _init_repo(tmp_path)
    git = Git(worktree)
    (worktree / "zeta.txt").write_text("z\n")
    (worktree / "alpha_to_delete.txt").write_text("a\n")
    await git.exec(["add", "--", "zeta.txt", "alpha_to_delete.txt"])
    anchor = await git.commit("anchor commit")

    await git.exec(["rm", "-q", "alpha_to_delete.txt"])
    (worktree / "beta_added_later.txt").write_text("b\n")
    await git.exec(["add", "--", "beta_added_later.txt"])
    await git.commit("later commit: delete alpha, add beta")

    at_anchor = await _tracked_at(worktree, anchor)
    at_head = await _tracked_at(worktree, "HEAD")

    assert at_anchor == ("alpha_to_delete.txt", "zeta.txt"), "sorted, and as of the anchor"
    assert at_head == ("beta_added_later.txt", "zeta.txt"), "sorted, and as of HEAD"
    assert "alpha_to_delete.txt" not in at_head
    assert "beta_added_later.txt" not in at_anchor


# =======================================================================================
# 4 — `_rewrite_targets`: only the relocated files at least one rule claims
# =======================================================================================


def _rule(rule_id: str, *, languages: list[str], applies_to: list[str]) -> RewriteRule:
    return RewriteRule(
        id=rule_id,
        languages=languages,
        applies_to=applies_to,
        rule={"pattern": "x"},
    )


def test_rewrite_targets_keeps_only_sources_at_least_one_rule_claims() -> None:
    """§3.2 step 2: a file no rule claims is not a target (RewriteWorker would report RULE_MISS
    on it). One TypeScript rule, three sources: one it claims, one it does not (wrong
    extension/glob), one a SECOND rule claims -- proving the `any(...)` OR-across-rules and the
    relocation via `relocated_path` both hold.

    Mutation target: `any(...)` -> `all(...)` would empty the result the moment two rules exist
    with disjoint `applies_to`; dropping `relocated_path` in favor of the bare source would return
    pre-relocation paths that no rule (matched against the post-relocation path) actually claims.
    """
    ts_rule = _rule("ts-rule", languages=["typescript"], applies_to=["**/*.ts"])
    py_rule = _rule("py-rule", languages=["python"], applies_to=["**/*.py"])
    sources = ["src/index.ts", "README.md", "src/util.py"]

    targets = _rewrite_targets("ts/acme/lib", sources, [ts_rule, py_rule])

    assert targets == ("ts/acme/lib/src/index.ts", "ts/acme/lib/src/util.py")
    assert "ts/acme/lib/README.md" not in targets


def test_rewrite_targets_is_empty_when_no_rule_matches_any_source() -> None:
    """The other side of the same branch: zero rules claim zero sources -- must not fall back to
    "every source is a target", which is the exact silent-relocation-only regression
    `_transform_rules`'s docstring describes for the sibling function."""
    ts_rule = _rule("ts-rule", languages=["typescript"], applies_to=["**/*.ts"])

    targets = _rewrite_targets("dest", ["README.md", "LICENSE"], [ts_rule])

    assert targets == ()


# =======================================================================================
# 5 — `_prepare_repo`: the `dest_path is None` refusal (untested branch)
# =======================================================================================


async def test_prepare_repo_refuses_when_layout_names_no_destination(
    tmp_path: Path,
) -> None:
    """§3.3's `layout(repo)` can REFUSE a destination (reserved `_scc/` namespace, or a `dest:`
    escaping the monorepo root); the caller signals that upstream by passing `dest_path=None`.
    This is `_prepare_repo`'s second precondition check and had no test anywhere in the suite.

    A bare `.git` directory (no real git repository) is sufficient: the check this test targets
    fires immediately after the worktree-existence check and before anything else touches git.

    Mutation target: dropping this `if dest_path is None: raise` (or its message) would send an
    absent-destination repo straight into `Git(worktree)` calls against a nonexistent git repo,
    surfacing as a confusing `GitError` instead of the actionable
    `TransformStepUnavailableError` this test pins.
    """
    write_config(tmp_path)
    settings = FleetSettings.load(tmp_path / "config")
    repo_id = "acme-no-dest"
    worktree = (settings.root / settings.config.run.work_dir).resolve() / repo_id
    (worktree / ".git").mkdir(parents=True)
    db_path = tmp_path / "state" / "fleet.db"
    _fresh_db(db_path)

    async with StateWriter(db_path, owner="test-batch38-no-dest") as writer:
        with pytest.raises(TransformStepUnavailableError) as raised:
            await _prepare_repo(
                settings,
                writer=writer,
                run_id=RUN_ID,
                repo_id=repo_id,
                dest_path=None,
                import_specifier=repo_id,
                rules=(),
                now=datetime.now(UTC),
            )

    assert "REFUSED a destination" in str(raised.value)
    assert repo_id in str(raised.value)


# =======================================================================================
# 6 — `_abandon_repo`: the `phase`/`kind` parameterization (untested with non-default values)
# =======================================================================================


async def test_abandon_repo_honors_a_non_default_phase_and_kind(tmp_path: Path) -> None:
    """`phase`/`kind` are parameters precisely because Phase 3's git preparation fails in the same
    *place* (per the function's own docstring) and must land against ITS OWN phase row and finding
    kind, not Phase 2's. No existing test ever passed non-default values for either.

    Mutation target: hardcoding `Phase.TRANSFORM`/`"TransformPreparationFailed"` in the SQL params
    (ignoring the parameters) would still pass every existing test, since none of them exercise a
    non-default value -- and would silently corrupt Phase 3's abandon path.
    """
    db_path = tmp_path / "fleet.db"
    _fresh_db(db_path)
    repo_id = "acme-build-fail"
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        _insert_repo(conn, repo_id, dest_path=None)
        conn.execute(
            "INSERT INTO phases (run_id, repo_id, phase, updated_at) VALUES (?, ?, ?, ?)",
            (RUN_ID, repo_id, int(Phase.BUILD), "2026-09-14T00:00:00Z"),
        )
    finally:
        conn.close()

    async with StateWriter(db_path, owner="test-batch38-abandon-phase") as writer:
        await _abandon_repo(
            writer,
            RUN_ID,
            repo_id,
            detail="clone merge failed",
            now=datetime.now(UTC),
            phase=Phase.BUILD,
            kind="BuildPreparationFailed",
        )

    conn = sqlite3.connect(db_path)
    try:
        phase_row = conn.execute(
            "SELECT status, failure_class FROM phases WHERE run_id = ? AND repo_id = ? "
            "AND phase = ?",
            (RUN_ID, repo_id, int(Phase.BUILD)),
        ).fetchone()
        finding_kind = conn.execute(
            "SELECT kind FROM findings WHERE run_id = ? AND repo_id = ?", (RUN_ID, repo_id)
        ).fetchone()
    finally:
        conn.close()

    assert phase_row == ("REQUIRES_HUMAN_INTERVENTION", "PREFLIGHT")
    assert finding_kind == ("BuildPreparationFailed",), (
        "the custom `kind` must reach the findings row, not the TRANSFORM default"
    )


# =======================================================================================
# 7 — `_wave_repos`: node_kind='REPO' filter + the `only` glob, applied in Python
# =======================================================================================


async def test_wave_repos_filters_contracts_and_applies_the_only_glob(tmp_path: Path) -> None:
    """Two properties in one query: contract nodes must never appear (the SQL's `node_kind =
    'REPO'`), and an `only` glob is applied on top of that in Python via `fnmatch`.

    Mutation target: inverting the `fnmatch` predicate, or dropping the `node_kind` filter, both
    change the returned set in a way this fixture (one repo that matches the glob, one that does
    not, one contract node) discriminates.
    """
    db_path = tmp_path / "fleet.db"
    _fresh_db(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        for repo_id in ("acme-billing", "acme-checkout", "acme-legacy"):
            _insert_repo(conn, repo_id, dest_path=None)
        conn.execute(
            "INSERT INTO waves (run_id, wave_index, computed_at) VALUES (?, 0, ?)",
            (RUN_ID, "2026-09-14T00:00:00Z"),
        )
        for kind, node_id in (
            ("REPO", "acme-billing"),
            ("REPO", "acme-checkout"),
            ("REPO", "acme-legacy"),
            ("CONTRACT", "proto:acme.commons.v1"),
        ):
            conn.execute(
                "INSERT INTO wave_members (run_id, wave_index, node_kind, node_id) "
                "VALUES (?, 0, ?, ?)",
                (RUN_ID, kind, node_id),
            )
    finally:
        conn.close()

    async with aiosqlite.connect(db_path) as aconn:
        unfiltered = await _wave_repos(aconn, RUN_ID, 0, None)
        filtered = await _wave_repos(aconn, RUN_ID, 0, "acme-b*")

    assert unfiltered == ("acme-billing", "acme-checkout", "acme-legacy"), (
        "no CONTRACT node may appear, and the result is sorted by node_id"
    )
    assert filtered == ("acme-billing",)


# =======================================================================================
# 8 — `_import_specifiers`: `suppress(ReservedDestError, ValueError)` excludes, never crashes
# =======================================================================================


async def test_import_specifiers_excludes_a_repo_layout_refuses_and_keeps_the_other(
    tmp_path: Path,
) -> None:
    """A repo whose destination `layout()` refuses (reserved `_scc/` namespace) must be silently
    absent from the result -- never raise out of the fleet-wide loop, and never appear with a
    specifier computed from a destination that does not exist. A normal repo alongside it must
    still get its real, adapter-computed specifier.

    Mutation target: dropping the `suppress(...)` (or narrowing it to the wrong exception type)
    would raise `ReservedDestError` out of the whole function, losing every repo's specifier for
    one bad apple; keeping BOTH repos in the result regardless would hide the failure entirely.
    """
    db_path = tmp_path / "fleet.db"
    _fresh_db(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        _insert_repo(conn, "acme-lib-ts", dest_path="ts/acme/lib", ecosystems='["npm"]')
        conn.execute(
            "INSERT INTO coordinates (coord_key, ecosystem, grp, name, owner_repo_id, "
            "                         first_seen_at) "
            "VALUES ('npm:@acme:lib', 'npm', '@acme', 'lib', 'acme-lib-ts', ?)",
            ("2026-09-14T00:00:00Z",),
        )
        conn.execute(
            "UPDATE repos SET primary_coord_key = 'npm:@acme:lib' WHERE repo_id = 'acme-lib-ts'"
        )
        _insert_repo(conn, "acme-reserved", dest_path="_scc/whatever", ecosystems='["npm"]')
    finally:
        conn.close()

    ecosystems.discover()  # idempotent; needed for `ecosystems.for_ecosystem(NPM)` below

    async with aiosqlite.connect(db_path) as aconn:
        specifiers = await _import_specifiers(aconn, {})

    assert specifiers == {"acme-lib-ts": "@acme/lib"}
    assert "acme-reserved" not in specifiers


# =======================================================================================
# 9 — `_dest_paths`: the REFUSED repo maps to `None` (present, not absent -- unlike siblings 8)
# =======================================================================================


async def test_dest_paths_maps_a_refused_repo_to_none_not_an_absent_key(
    tmp_path: Path,
) -> None:
    """The one behavioral difference from `_import_specifiers`, worth pinning explicitly: a repo
    `layout()` refuses is a PRESENT key with value `None` here (§3.3's caller reports it per
    repo), never dropped from the dict the way `_import_specifiers` drops it.

    Mutation target: `except (...): out[repo_id] = None` -> `except (...): continue` would drop
    the key instead of setting it `None`, and the caller (which iterates the dict to decide which
    repos to report as REFUSED) would silently stop seeing that repo at all.
    """
    db_path = tmp_path / "fleet.db"
    _fresh_db(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        _insert_repo(conn, "acme-lib-ts", dest_path="ts/acme/lib")
        _insert_repo(conn, "acme-reserved", dest_path="_scc/whatever")
    finally:
        conn.close()

    async with aiosqlite.connect(db_path) as aconn:
        dests = await _dest_paths(aconn, {})

    assert dests["acme-lib-ts"] == "ts/acme/lib"
    assert "acme-reserved" in dests, "a refused repo must be a present key, not dropped"
    assert dests["acme-reserved"] is None


# =======================================================================================
# 10 — `_ordering_pairs`: the three-way SQL filter + the (dst, src) -> (dependency, dependent)
#      column-order swap
# =======================================================================================


async def test_ordering_pairs_applies_all_three_filters_and_reverses_the_edge_direction(
    tmp_path: Path,
) -> None:
    """`edges` stores dependent -> dependency; `_ordering_pairs` must return the pair REVERSED as
    `(dependency, dependent)`. Four edges seeded, only one of which should survive: the other
    three each fail exactly one of the three filter clauses (`ordering_suppressed = 0`,
    `confidence >= min_confidence`, `kind IN (dag_edge_kinds)`) -- default settings are
    `min_confidence=0.5` and `dag_edge_kinds` including `DECLARED_DEP` but not `SHARED_RESOURCE`.

    Mutation target: `SELECT dst_id, src_id` -> `SELECT src_id, dst_id` (column order) would swap
    every dependency/dependent in the surviving pair without changing the row COUNT, so a test
    that only checked `len(pairs) == 1` would not catch it -- this test checks the exact tuple.
    Dropping any one of the three `WHERE` clauses would let its corresponding excluded edge
    through, changing the count from 1 to 2.
    """
    db_path = tmp_path / "fleet.db"
    _fresh_db(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        for repo_id in ("acme-billing", "acme-commons", "acme-suppressed-dep",
                        "acme-low-conf-dep", "acme-wrong-kind-dep"):
            _insert_repo(conn, repo_id, dest_path=None)

        def edge(
            edge_key: str,
            src: str,
            dst: str,
            *,
            kind: str = "DECLARED_DEP",
            confidence: float = 0.95,
            ordering_suppressed: int = 0,
        ) -> None:
            conn.execute(
                "INSERT INTO edges (edge_key, run_id, src_kind, src_id, dst_kind, dst_id, "
                "                   dst_coord_key, kind, base_confidence, confidence, "
                "                   ordering_suppressed, evidence_path, detected_at) "
                "VALUES (?, ?, 'REPO', ?, 'REPO', ?, ?, ?, ?, ?, ?, 'pom.xml', ?)",
                (edge_key, RUN_ID, src, dst, f"maven:x:{dst}", kind, confidence, confidence,
                 ordering_suppressed, "2026-09-14T00:00:00Z"),
            )

        # Qualifying: acme-billing (dependent) depends on acme-commons (dependency).
        edge("a" * 64, "acme-billing", "acme-commons")
        # Excluded: suppressed feedback edge.
        edge("b" * 64, "acme-billing", "acme-suppressed-dep", ordering_suppressed=1)
        # Excluded: below the default min_confidence (0.5).
        edge("c" * 64, "acme-billing", "acme-low-conf-dep", confidence=0.2)
        # Excluded: a kind not in the default `dag_edge_kinds`.
        edge("d" * 64, "acme-billing", "acme-wrong-kind-dep", kind="SHARED_RESOURCE")
    finally:
        conn.close()

    write_config(tmp_path)
    settings = FleetSettings.load(tmp_path / "config")

    async with aiosqlite.connect(db_path) as aconn:
        pairs = await _ordering_pairs(aconn, settings, RUN_ID)

    assert pairs == (("acme-commons", "acme-billing"),), (
        "exactly the one qualifying edge, as (dependency, dependent) -- reversed from storage "
        "order"
    )
