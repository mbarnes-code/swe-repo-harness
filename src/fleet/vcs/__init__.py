"""Version-control drivers: async git, git-filter-repo ingest, and the `gh` CLI wrapper (ADR-0011).

Three boundaries, one rule: **git is the record of code state, SQLite is the record of
orchestration state** (ADR-0024). Nothing here writes SQL — every function returns a value the
single writer (§11.5) persists — and nothing here builds a command as a string: every invocation is
an argv list handed to `util.proc.run`, so there is no shell anywhere in this package.

* `git.py` — the typed git surface (`Git`), structured results, redacted errors.
* `commits.py` — one commit per task, the `Fleet-Patch-Id` trailer, the two-condition guard, the
  two distinct rollback anchors (§3.2 step 6), and `revert_and_commit` — the `git revert -m 1` +
  `Fleet-*`-trailer primitive SPEC §3.1's hoist rollback needs (D111 Leg B; not yet wired into any
  caller).
* `filter_repo.py` — history-rewriting ingest, the integration mutex, immutable snapshot refs
  (§3.3 step 1).
* `forge.py` — the `Forge` Protocol the PR path depends on, and the PR types both drivers share.
* `github.py` — PR creation and PR *state ingestion*, without which `MERGED` is never written and
  the fleet deadlocks at the first wave boundary (§3.4 step 5).
* `gitea.py` — the same surface against a self-hosted Gitea, over `curl -K` so the token never
  reaches argv.

`build_forge` lives here, rather than in `forge.py`, because a factory that names both drivers
would otherwise import the modules that import the Protocol. This package already imports both, so
the wiring costs no cycle and no deferred import.
"""

from __future__ import annotations

from pathlib import Path

from fleet.util.proc import CommandRunner
from fleet.vcs.commits import (
    CommitOutcome,
    FleetTrailers,
    GuardOutcome,
    RevertOutcome,
    apply_and_commit,
    discard_task,
    find_task_commit,
    guard,
    patch_id,
    record_task_anchor,
    revert_and_commit,
    rollback_phase,
)
from fleet.vcs.filter_repo import (
    IngestResult,
    IntegrationMutex,
    RelocationSpec,
    SnapshotRef,
    SourceProvenance,
    ingest,
    integration_snapshot,
    relocate,
)
from fleet.vcs.forge import (
    FORGE_NAMES,
    NON_TERMINAL_STATES,
    Forge,
    ForgeError,
    ForgeUnavailableError,
    PrStatus,
    PrSyncItem,
)
from fleet.vcs.git import (
    CommitInfo,
    DiffStat,
    FileStat,
    Git,
    GitCommandError,
    GitError,
    GitRefError,
)
from fleet.vcs.gitea import GiteaError, GiteaForge, GiteaUnavailableError
from fleet.vcs.github import GhError, GitHubCli, parse_pr_view

__all__ = [
    "FORGE_NAMES",
    "NON_TERMINAL_STATES",
    "CommitInfo",
    "CommitOutcome",
    "DiffStat",
    "FileStat",
    "FleetTrailers",
    "Forge",
    "ForgeError",
    "ForgeUnavailableError",
    "GhError",
    "Git",
    "GitCommandError",
    "GitError",
    "GitHubCli",
    "GitRefError",
    "GiteaError",
    "GiteaForge",
    "GiteaUnavailableError",
    "GuardOutcome",
    "IngestResult",
    "IntegrationMutex",
    "PrStatus",
    "PrSyncItem",
    "RelocationSpec",
    "RevertOutcome",
    "SnapshotRef",
    "SourceProvenance",
    "apply_and_commit",
    "build_forge",
    "discard_task",
    "find_task_commit",
    "guard",
    "ingest",
    "integration_snapshot",
    "parse_pr_view",
    "patch_id",
    "record_task_anchor",
    "relocate",
    "revert_and_commit",
    "rollback_phase",
]


def build_forge(
    forge: str,
    *,
    runner: CommandRunner | None = None,
    base_url: str = "",
    owner: str = "",
    repo: str | None = None,
    curl_config: Path | None = None,
    gh_bin: str = "gh",
    cwd: Path | None = None,
    deadline: float | None = None,
) -> Forge:
    """Build the driver `pr.forge` names. The one place the harness chooses a code host.

    `runner=None` means "execute the real binary": `GitHubCli` and `GiteaForge` both default to
    `util.proc.run`, and passing `None` through would replace that default with a `None` that fails
    at the first call. That is the same `GH_RUNNER` seam `cli.py` already threads through.

    An unknown name raises here as well as at settings-validation time. Settings is where an
    operator's typo is caught; this raise is what stops a programmatic caller from getting a
    `None`-shaped forge and discovering it at the first PR.
    """
    if forge == "github":
        if runner is not None:
            return GitHubCli(runner=runner, gh_bin=gh_bin, cwd=cwd, deadline=deadline)
        return GitHubCli(gh_bin=gh_bin, cwd=cwd, deadline=deadline)
    if forge == "gitea":
        if curl_config is None:
            raise ForgeError(
                "the gitea forge needs `pr.forge_token_config` — the mode-600 `curl -K` file "
                "holding the `Authorization: token …` header. It is a file, not a setting, "
                "precisely so the token never enters argv (§11.4)"
            )
        if runner is not None:
            return GiteaForge(
                owner=owner,
                base_url=base_url,
                curl_config=curl_config,
                repo=repo,
                runner=runner,
                cwd=cwd,
                deadline=deadline,
            )
        return GiteaForge(
            owner=owner,
            base_url=base_url,
            curl_config=curl_config,
            repo=repo,
            cwd=cwd,
            deadline=deadline,
        )
    raise ForgeError(
        f"unknown forge {forge!r}; known forges are {sorted(FORGE_NAMES)}. "
        "Choosing one silently would open PRs against the wrong host"
    )
