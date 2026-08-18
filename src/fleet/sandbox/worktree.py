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

**Reaping is best-effort per entry, and says so.** `remove()` can legitimately raise
(`WorktreeError`, D44: an unsettled probe must not license an `rmtree`), and one entry raising
must not discard the removals `reap()` already made, nor stop it from attempting the rest — a
`reap()` that aborts on the first `WorktreeError` trades D44's over-deletion for silent
under-reaping: every worktree still to be swept in that iteration order leaks until the next
`fleet resume`. It also must not paper over the failure by returning as if nothing went wrong —
a caller that treats a `list[str]` as "everything that needed reaping is gone" would be misled by
a silently partial result, which is the same four-state-collapse shape D44 belongs to, one layer
up. So `reap()` returns a `ReapResult`: `reaped` for what it actually removed, `failed` for what
it attempted and could not verify gone (each with the reason). A worktree spared because its
owner is still live never appears in either list — "deliberately not attempted" and "attempted
and unresolved" are different facts and must not collapse into one.

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

from fleet.util.proc import CommandRunner, ProcResult, no_verdict, run

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


@dataclass(frozen=True, slots=True)
class ReapFailure:
    """One `reap()` entry that was attempted and could not be verified gone — distinct from a
    worktree `reap()` never attempted because its owner is still live (SPEC §11.5)."""

    name: str
    reason: str


@dataclass(frozen=True, slots=True)
class ReapResult:
    """The outcome of one `reap()` sweep, kept honest about partial success (Rule 9/Rule 11).

    `reaped` names worktrees this call actually removed. `failed` names worktrees it attempted
    and could not — each `remove()` failure is self-healing (the entry is still registered and
    still absent from `live_names`, so the NEXT `reap()` sweep retries it), but a caller must be
    able to tell "everything reaped" from "some entries are still stuck" rather than reading a
    bare list and assuming completeness.
    """

    reaped: list[str]
    failed: list[ReapFailure]

    @property
    def complete(self) -> bool:
        """True iff every dead worktree this sweep found was actually removed."""
        return not self.failed


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
        try:
            result = await self._git_run(
                ["worktree", "remove", "--force", str(path)],
                deadline=deadline,
                timeout_s=timeout_s,
            )
        except OSError as exc:
            # `_git_run` → `self._runner` (by default `util/proc.run`) never wraps the spawn
            # itself: `asyncio.create_subprocess_exec` is called unguarded (`_run_locked`), so a
            # missing `git` binary (`FileNotFoundError`), `PermissionError` on `cwd`, or resource
            # exhaustion propagates straight out as an `OSError`. That is an environment fault —
            # git was never even invoked — and is a materially different fact from a *settled*
            # git-level refusal (the `no_verdict`/`result.ok` branches below): the wording here
            # must stay distinguishable in `ReapFailure.reason` so a caller reading `reap()`'s
            # output can tell "the machine could not run git" from "git looked and said no".
            raise WorktreeError(
                f"git worktree remove for {path} never ran: environment fault spawning "
                f"{self._git!r} ({type(exc).__name__}: {exc}), not a git-level refusal"
            ) from exc
        # git refuses a path it does not know as a worktree. If the directory is gone (or never
        # existed) that is success; if it is still there, git's refusal is real — but "git's
        # refusal is real" is exactly what an unsettled `result` does NOT establish (D44): a
        # `worktree remove --force` that never started (deadline already passed) or was killed
        # mid-operation was never consulted, and treating that silence as a refusal would
        # `rmtree` a still-registered worktree out from under git. `no_verdict` must be checked
        # BEFORE `result.ok` for the same reason it is everywhere else in this family — a call
        # made past an already-passed deadline reports both `started=False` and `timed_out=True`
        # at once, and reading `timed_out` first would misreport a command that never ran as one
        # that merely took too long.
        reason = no_verdict(result)
        if reason is not None:
            raise WorktreeError(
                f"git worktree remove for {path} did not settle, so its refusal cannot be "
                f"trusted as license to delete the directory: {reason}"
            )
        if not result.ok and path.exists():
            shutil.rmtree(path, ignore_errors=True)
            if path.exists():
                raise WorktreeError(
                    f"could not remove worktree {path} (exit {result.exit_code}): "
                    f"{result.stderr_tail}"
                )
        # Drops the administrative record left behind by a directory deleted out from under git.
        # Same environment-fault guard as above — this is a second, independent subprocess spawn.
        try:
            await self._git_run(["worktree", "prune"], deadline=deadline, timeout_s=timeout_s)
        except OSError as exc:
            raise WorktreeError(
                f"git worktree prune for {self.repo_dir} never ran: environment fault spawning "
                f"{self._git!r} ({type(exc).__name__}: {exc}), not a git-level refusal"
            ) from exc
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
    ) -> ReapResult:
        """Remove every `fleet-<run_id>-*` worktree NOT in `live_names` (SPEC §11.5 step 2).

        `live_names` is required rather than defaulted: a reaper that defaults to "nothing is
        live" deletes the checkout of a task that is mid-`git apply`. The caller derives it from
        the `phases` rows that still hold a lease.

        One entry's `remove()` raising `WorktreeError` (D44: an unsettled probe) does not stop
        the sweep — every other dead entry is still attempted — and does not silently disappear:
        it is recorded in the returned `ReapResult.failed` rather than discarded. Aborting on the
        first failure would leak every worktree still to come in iteration order until the next
        `fleet resume`; swallowing the failure and reporting only `reaped` would let a caller
        believe a partial sweep was a complete one. Both are instances of the same collapse D44
        was named for, so neither is acceptable here.
        """
        live = set(live_names)
        prefix = run_prefix(self.run_id)
        reaped: list[str] = []
        failed: list[ReapFailure] = []
        for path in await self.list_registered(deadline=deadline, timeout_s=timeout_s):
            name = path.name
            if not name.startswith(prefix) or name in live:
                continue
            try:
                await self.remove(path, deadline=deadline, timeout_s=timeout_s)
            except WorktreeError as exc:
                failed.append(ReapFailure(name=name, reason=str(exc)))
                continue
            reaped.append(name)
        return ReapResult(reaped=reaped, failed=failed)
