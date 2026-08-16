"""git worktree lifecycle: creation, naming, reaping (ADR-0010, SPEC §3.2/§3.3/§11.5).

**A worktree has exactly one owning task at a time.** That single-owner rule (SPEC §3.2 step 4:
"Nothing else runs against this worktree concurrently") is what makes the Git-native resume
contract meaningful — if two tasks could write one checkout, "did this task's commit land?" would
have no answer. It is enforced here structurally: the name encodes the owner
(`fleet-<run_id>-<repo>-<attempt>`, SPEC §3.3), and `create()` refuses a name that already exists
rather than handing out a second reference to it.

**Reaping must be crash-safe and owner-aware.** `fleet resume` step 2 reaps worktrees named
`fleet-<run_id>-*` "that no live `phases` row claims" (SPEC §11.5) — so `reap()` takes the live
set explicitly and will not remove a worktree a running task still owns. Symmetrically,
`remove()` treats an already-gone worktree as success: after a crash the directory may be half
deleted, or deleted with git's administrative record left behind, and a cleanup path that raises
on that turns one crash into a permanently un-resumable run.

This module owns the naming scheme for BOTH isolation primitives; `sandbox/container.py` imports
`sandbox_name` so a container and the worktree it bind-mounts can never drift apart.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from fleet.util.proc import CommandRunner, ProcResult, run

NAME_PREFIX = "fleet"
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def slug(value: str) -> str:
    """`acme/commons` → `acme-commons`. Docker container names and directory names accept a
    narrower alphabet than a repo id does, and the same slug must serve both."""
    cleaned = _UNSAFE.sub("-", value).strip("-")
    return cleaned or "unnamed"


def run_prefix(run_id: UUID | str) -> str:
    """`fleet-<run_id>-` — the glob `fleet resume` and `fleet gc` reap by (SPEC §11.5)."""
    return f"{NAME_PREFIX}-{slug(str(run_id))}-"


def sandbox_name(run_id: UUID | str, repo: str, attempt: int) -> str:
    """`fleet-<run_id>-<repo>-<attempt>` (SPEC §3.3). One string names the worktree directory and
    the container, so an orphan of either kind is attributable to a task without a database."""
    if attempt < 0:
        raise ValueError(f"attempt must be non-negative, got {attempt}")
    return f"{run_prefix(run_id)}{slug(repo)}-{attempt}"


@dataclass(frozen=True, slots=True)
class Worktree:
    """A checkout handed to exactly one task, with the identity needed to reap it later."""

    name: str
    path: Path
    repo_dir: Path
    ref: str


class WorktreeError(RuntimeError):
    """A worktree operation failed for a reason that is NOT "it was already gone" (Rule 11)."""


class WorktreeManager:
    """Creates, hands out, and reaps the worktrees of one run.

    `runner` is injected (CLAUDE.md guardrail 3) so every git invocation inherits the caller's
    `ctx.deadline` and the `limits.subprocess` semaphore, and so command construction is testable.
    """

    def __init__(
        self,
        *,
        repo_dir: Path,
        work_dir: Path,
        run_id: UUID | str,
        runner: CommandRunner = run,
        git_bin: str = "git",
    ) -> None:
        self.repo_dir = Path(repo_dir)
        self.work_dir = Path(work_dir)
        self.run_id = run_id
        self._runner = runner
        self._git = git_bin

    async def _git_run(
        self, args: Sequence[str], *, deadline: float | None, timeout_s: float | None
    ) -> ProcResult:
        return await self._runner(
            [self._git, "-C", str(self.repo_dir), *args],
            deadline=deadline,
            timeout_s=timeout_s,
        )

    def path_for(self, repo: str, attempt: int) -> Path:
        return self.work_dir / sandbox_name(self.run_id, repo, attempt)

    async def create(
        self,
        repo: str,
        attempt: int,
        ref: str,
        *,
        deadline: float | None = None,
        timeout_s: float | None = 300.0,
    ) -> Worktree:
        """Cut `fleet-<run_id>-<repo>-<attempt>` at `ref` and hand it to its one owning task.

        `--detach` because the worktree is a disposable view of a commit: the branch
        (`migrate/<repo>`) is created and advanced by `vcs/commits.py`, and two worktrees holding
        the same branch checked out is exactly the collision the single-owner rule forbids.
        """
        name = sandbox_name(self.run_id, repo, attempt)
        path = self.work_dir / name
        if path.exists():
            raise WorktreeError(
                f"worktree {name} already exists at {path}: a worktree has exactly one owning "
                "task (§11.5); reap the previous owner before re-creating it"
            )
        self.work_dir.mkdir(parents=True, exist_ok=True)
        result = await self._git_run(
            ["worktree", "add", "--detach", str(path), ref],
            deadline=deadline,
            timeout_s=timeout_s,
        )
        if not result.ok:
            raise WorktreeError(
                f"git worktree add failed (exit {result.exit_code}) for {name} at {ref}: "
                f"{result.stderr_tail}"
            )
        return Worktree(name=name, path=path, repo_dir=self.repo_dir, ref=ref)

    async def remove(
        self,
        target: Worktree | Path | str,
        *,
        deadline: float | None = None,
        timeout_s: float | None = 120.0,
    ) -> bool:
        """Remove a worktree. Returns True if this call removed one, False if it was already gone.

        Idempotent on purpose: cleanup runs after crashes, after `on_cancel`, and again at
        `fleet resume`, so "already removed" is the expected case, not an error.
        """
        path = self._resolve(target)
        existed = path.exists()
        result = await self._git_run(
            ["worktree", "remove", "--force", str(path)],
            deadline=deadline,
            timeout_s=timeout_s,
        )
        # git refuses a path it does not know as a worktree. If the directory is gone (or never
        # existed) that is success; if it is still there, git's refusal is real.
        if not result.ok and path.exists():
            shutil.rmtree(path, ignore_errors=True)
            if path.exists():
                raise WorktreeError(
                    f"could not remove worktree {path} (exit {result.exit_code}): "
                    f"{result.stderr_tail}"
                )
        # Drops the administrative record left behind by a directory deleted out from under git.
        await self._git_run(["worktree", "prune"], deadline=deadline, timeout_s=timeout_s)
        return existed

    def _resolve(self, target: Worktree | Path | str) -> Path:
        if isinstance(target, Worktree):
            return target.path
        if isinstance(target, Path):
            return target
        return self.work_dir / target

    async def list_registered(
        self, *, deadline: float | None = None, timeout_s: float | None = 60.0
    ) -> list[Path]:
        """Every worktree git currently records for this repo, main checkout excluded."""
        result = await self._git_run(
            ["worktree", "list", "--porcelain"], deadline=deadline, timeout_s=timeout_s
        )
        if not result.ok:
            raise WorktreeError(
                f"git worktree list failed (exit {result.exit_code}): {result.stderr_tail}"
            )
        paths: list[Path] = []
        for line in result.stdout_tail.splitlines():
            if line.startswith("worktree "):
                paths.append(Path(line[len("worktree ") :].strip()))
        main = self.repo_dir.resolve()
        return [p for p in paths if p.resolve() != main]

    async def reap(
        self,
        *,
        live_names: Iterable[str],
        deadline: float | None = None,
        timeout_s: float | None = 120.0,
    ) -> list[str]:
        """Remove every `fleet-<run_id>-*` worktree NOT in `live_names` (SPEC §11.5 step 2).

        `live_names` is required rather than defaulted: a reaper that defaults to "nothing is
        live" deletes the checkout of a task that is mid-`git apply`. The caller derives it from
        the `phases` rows that still hold a lease.
        """
        live = set(live_names)
        prefix = run_prefix(self.run_id)
        reaped: list[str] = []
        for path in await self.list_registered(deadline=deadline, timeout_s=timeout_s):
            name = path.name
            if not name.startswith(prefix) or name in live:
                continue
            await self.remove(path, deadline=deadline, timeout_s=timeout_s)
            reaped.append(name)
        return reaped
