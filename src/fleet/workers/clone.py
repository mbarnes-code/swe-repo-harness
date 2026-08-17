"""Phase 1 step 1: git mirror + worktree materialization, plus the preflight gate (§3.1 step 1).

Records `is_shallow`, `submodule_count`, `has_lfs`, `largest_blob_bytes`, `head_sha`,
`default_branch` and `default_branch_source`; strips any credential from the URL before it is
ever persisted, logged, or attached to an error (§11.4).

Three properties are load-bearing here, and each is a defect this module exists to make
unreachable:

* **The credential never leaves this function.** `git clone` is handed the URL the operator
  configured — it has to be, or a private mirror cannot be fetched — and the very next thing the
  worker does is rewrite the mirror's `origin` to the credential-free form. Nothing this module
  returns, logs, or raises carries userinfo: `CloneOutput.url` is the stripped URL, every
  `WorkerError` tail goes through `redact_text`, and `GitCommandError` redacts its own argv.
* **Re-entry is incremental, not a replay.** An existing mirror is `git remote update`d rather
  than re-cloned (§3.1 step 1), and an existing, valid worktree is left exactly as it is. That is
  what makes `fleet scan` cheap to re-run, and it is why `preconditions_hold` interrogates the
  filesystem — mirror `HEAD`, worktree `.git`, a resolvable commit — rather than trusting a row.
* **Every preflight check that got an answer is a gate, never a crash.** A repo whose shape the
  harness cannot handle returns a non-retryable `PREFLIGHT` `WorkerError`, which `execute()` turns
  into `REQUIRES_HUMAN_INTERVENTION` for that repo while the fleet continues. An *empty* repo is
  not a failure at all (§3.1: "SKIPPED, not an error"): it returns `ok` with `preflight_ok = False`
  and an `EmptyRepo` finding, and no worktree is cut because there is no commit to cut one from.
* **A probe that got no answer reports no verdict at all.** Every preflight number and finding
  here is published as a *measurement* — `repos.submodule_count`, `repos.has_lfs`,
  `repos.largest_blob_bytes`, an `EmptyRepo` row — and a measurement is only as true as the
  command that produced it. `util.proc.run` reports a deadline that passed before the call as
  `timed_out=True` and `started=False` and `exit_code=124` together, so `if not result.ok` cannot
  tell "the repo says no" from "we never asked". `_no_verdict` draws that line once, and every
  probe that could otherwise fabricate a zero, a `False`, or a `PREFLIGHT` gate out of silence
  raises through `_error_for` instead, which hands both flags to `base.clock_failure` — the one
  function that decides between free `TRANSIENT_INFRA` for a command that never ran and
  substantive `TIMEOUT` for one killed at its deadline, and the same function
  `buildverify.classify_build_failure` calls, so the two workers cannot answer differently for the
  same `ProcResult`. Either way it is retryable, because the next attempt genuinely can produce
  the answer this one did not.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Final
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field

from fleet.models.enums import FailureClass, Phase
from fleet.models.repo import RepoId
from fleet.obs.redact import redact_text
from fleet.orchestrator.registry import register_worker
from fleet.sandbox.worktree import slug
from fleet.util.fs import DiskFloorBreached, require_free_space, scoped_tempdir
from fleet.util.proc import CommandRunner, ProcResult
from fleet.util.proc import run as proc_run
from fleet.vcs.git import Git, GitCommandError, GitError
from fleet.workers.base import (
    BaseWorker,
    WorkerContext,
    WorkerError,
    WorkerInput,
    WorkerOutput,
    WorkerResult,
    clock_failure,
    loop_now,
)

__all__ = [
    "UNITS",
    "CloneInput",
    "CloneOutput",
    "CloneWorker",
    "credential_free",
]

UNITS: Final[tuple[str, ...]] = ("mirror", "worktree")
"""The two units this worker lands, in order. Small and fixed: the expensive half is the network
fetch, and a checkpoint that says "the mirror is there" is what stops a re-scan re-fetching it."""

_BRANCH_FALLBACKS: Final[tuple[str, ...]] = ("main", "master", "trunk", "develop")


def credential_free(url: str) -> str:
    """The URL with any userinfo removed — `https://oauth2:ghp_x@host/a.git` → `https://host/a.git`.

    Deliberately NOT `redact_text`: the placeholder is for *rendering* a URL, and this value is
    the one written to `repos.url` and set as the mirror's `origin`, so it has to remain a URL
    git can use. §3.1 step 1 requires exactly this rewrite "before any other step".
    """
    parts = urlsplit(url)
    if not parts.netloc or "@" not in parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


class CloneInput(WorkerInput):
    """What one repo's clone needs. `url` MAY carry userinfo; nothing else in the system may."""

    repo_id: RepoId
    url: str = Field(min_length=1, description="As configured; stripped before it is persisted")
    cache_dir: str = Field(min_length=1, description="`cache/git`; the mirror lands under it")
    worktree_path: str | None = Field(
        default=None, description="Override for `ctx.workdir`, which is the default"
    )
    branch_fallbacks: tuple[str, ...] = _BRANCH_FALLBACKS
    max_repo_bytes: int = Field(default=5_368_709_120, gt=0)
    max_blob_bytes: int = Field(default=104_857_600, gt=0)
    min_free_bytes: int = Field(
        default=0,
        ge=0,
        description="`preflight.min_free_bytes`, re-checked before THIS clone rather than once at "
        "startup (§11.3): the fleet fills the volume as it runs, so a floor tested at repo 1 says "
        "nothing about repo 180. `0` disables the gate and is the default only because a payload "
        "built by hand in a test has no `config/fleet.yaml` behind it; `fleet scan` always passes "
        "the configured value.",
    )
    unshallow: bool = True
    require_lfs_binary: bool = True
    remaining_units: tuple[str, ...] | None = Field(
        default=None,
        description="The checkpoint's owed units; None = no checkpoint, run the phase whole",
    )


class CloneOutput(WorkerOutput):
    """The `repos` columns §3.1 step 1 names, plus the two paths later scan steps re-enter."""

    repo_id: RepoId
    url: str = Field(description="Credential-free by construction; see `credential_free`")
    mirror_path: str
    worktree_path: str | None = None
    default_branch: str = ""
    default_branch_source: str = "symbolic-ref"
    head_sha: str | None = None
    commit_count: int = Field(default=0, ge=0)
    size_bytes: int = Field(default=0, ge=0)
    is_shallow: bool = False
    submodule_count: int = Field(default=0, ge=0)
    has_lfs: bool = False
    largest_blob_bytes: int = Field(default=0, ge=0)
    preflight_ok: bool = False
    findings: tuple[str, ...] = ()
    """`findings` KINDS only (`EmptyRepo`, `OversizeBlob`, `SubmodulePresent`). The row itself is
    the runner's to write — a worker writes no SQL (§11.5)."""


@dataclass(frozen=True, slots=True)
class _Preflight:
    """Every §3.1 step 1 probe's answer, plus the gate that failed (if one did)."""

    default_branch: str
    default_branch_source: str
    head_sha: str | None
    commit_count: int
    size_bytes: int
    is_shallow: bool
    submodule_count: int
    has_lfs: bool
    largest_blob_bytes: int
    findings: tuple[str, ...]
    gate: str | None = None


def _dir_size(root: Path) -> int:
    """`du` for the mirror, without a subprocess. Symlinks are not followed: a mirror holding one
    is not entitled to charge the fleet for whatever it points at."""
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        total += path.stat().st_size
    return total


def _largest_blob(stdout_path: Path) -> int:
    """Stream `cat-file --batch-check` output and keep only the running maximum.

    Read line by line from the file `util.proc` already wrote rather than from a captured string:
    a fleet-sized mirror emits millions of lines, and §11.3 forbids holding any of it resident.
    """
    largest = 0
    with stdout_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            fields = line.split()
            if len(fields) < 3 or fields[1] != "blob":
                continue
            try:
                size = int(fields[2])
            except ValueError:  # pragma: no cover - git does not emit a non-numeric size
                continue
            largest = max(largest, size)
    return largest


def _no_verdict(result: ProcResult) -> str | None:
    """Why `result` establishes NOTHING about the repo — or `None` when it is a real answer.

    `ProcResult.ok` is `started and not timed_out and exit_code == 0`, and `util.proc.run`
    synthesises a deadline that had already passed as `timed_out=True` **and** `started=False`
    **and** `exit_code=124`, all three at once. So a bare `if not result.ok` collapses four
    distinct causes into one branch: the command never ran, the command was killed at the
    deadline, the command ran and exited non-zero, and — for the probes below that ask a
    yes/no question — the command ran and the answer was legitimately *no*.

    Only the last two are facts about the repository. The first two are facts about the fleet's
    clock, and a preflight verdict derived from them is a verdict nobody established. Every
    caller here therefore asks this first, and treats a non-`None` answer as "we did not find
    out" rather than as an answer.

    `started` is tested BEFORE `timed_out` deliberately: a call made past the deadline carries
    both flags, so reading `timed_out` first would report a command that never ran as one that
    ran too long — the same misattribution, one layer down.
    """
    if not result.started:
        return "the command was never started: the deadline had already passed"
    if result.timed_out:
        return f"the command was killed at its deadline (exit {result.exit_code})"
    return None


def _indeterminate(result: ProcResult, reason: str, *, cwd: Path) -> GitCommandError:
    """The exception a probe with no verdict raises.

    A `GitCommandError` rather than a preflight gate string, because `run()`'s handler routes it
    through `_error_for`, which answers `TIMEOUT` / `TRANSIENT_INFRA` with `retryable=True`. A gate
    would instead answer non-retryable `PREFLIGHT` — "the repo's own shape; identical on every
    attempt" — which is exactly the claim a probe that produced no output is in no position to
    make.

    **`started` is forwarded, not folded into `timed_out`.** `_no_verdict` above separates "never
    spawned" from "killed at the deadline"; carrying only `timed_out` — which `util.proc.run` sets
    for BOTH — threw that separation away one line after it was drawn, and `_error_for` then
    answered substantive `TIMEOUT` for a measurement nobody took, charging the repo an ADR-0014
    rung for it. The two flags travel together from here on.
    """
    tail = f"{reason}: {result.stderr_tail}".strip().rstrip(":")
    return GitCommandError(
        result.argv,
        result.exit_code,
        tail,
        cwd=cwd,
        timed_out=result.timed_out,
        started=result.started,
    )


def _owed(units: Sequence[str], remaining: Sequence[str] | None) -> list[str]:
    """The units this invocation still owes: everything, or the checkpoint's remainder."""
    if remaining is None:
        return list(units)
    owed = set(remaining)
    return [unit for unit in units if unit in owed]


def _iter_submodule_names(text: str) -> Iterator[str]:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[submodule "):
            yield stripped


@register_worker
class CloneWorker(BaseWorker[CloneInput, CloneOutput]):
    """`git clone --mirror` + detached worktree + the preflight gate (§3.1 step 1)."""

    __slots__ = ("_runner",)

    name: ClassVar[str] = "clone"
    phase: ClassVar[Phase] = Phase.SCAN
    input_model: ClassVar[type[WorkerInput]] = CloneInput
    output_model: ClassVar[type[WorkerOutput]] = CloneOutput

    def __init__(self, runner: CommandRunner = proc_run) -> None:
        """`runner` is injected (Guardrail 3) so a test can observe the argv a real run executes.

        Held in a `__slots__` member rather than an instance dict: a registry value is a shared
        singleton (§7.2), and `assert_stateless` must stay true of it.
        """
        self._runner = runner

    # -- the §7.1 contract ---------------------------------------------------------------

    async def preconditions_hold(self, ctx: WorkerContext, payload: CloneInput) -> bool:
        """Is the mirror-plus-worktree state this payload would produce already on disk?

        Three filesystem facts, in cost order, because this runs before every re-entry: the
        mirror is a git directory, the worktree is a git worktree, and the worktree resolves a
        commit. `False` means "materialize it" — for a fresh repo, for a mirror whose worktree
        was reaped, and for the half-created directory a killed `git worktree add` leaves behind,
        all three of which must re-run rather than be trusted.
        """
        mirror = self._mirror_path(payload)
        worktree = self._worktree_path(ctx, payload)
        if not await asyncio.to_thread(_mirror_is_initialized, mirror):
            return False
        if not await asyncio.to_thread(_worktree_is_initialized, worktree):
            return False
        return await self._git(worktree, ctx).resolve("HEAD") is not None

    async def run(self, ctx: WorkerContext, payload: CloneInput) -> WorkerResult[CloneOutput]:
        mirror = self._mirror_path(payload)
        worktree = self._worktree_path(ctx, payload)
        owed = _owed(UNITS, payload.remaining_units)
        completed: list[str] = [unit for unit in UNITS if unit not in owed]
        clean_url = credential_free(payload.url)

        try:
            if "mirror" in owed:
                # §11.3: BEFORE the clone, every time. A mirror is the single largest thing this
                # harness writes, and the volume it lands on is shared with the state DB whose
                # `BEGIN IMMEDIATE` is what an ENOSPC would corrupt (§13 row 42).
                require_free_space(
                    mirror.parent,
                    payload.min_free_bytes,
                    operation=f"git clone --mirror {payload.repo_id}",
                )
                await self._materialize_mirror(ctx, payload, mirror, clean_url)
                completed.append("mirror")
            if ctx.cancelled():
                return self._interrupted(completed, owed)

            preflight = await self._preflight(ctx, payload, mirror)
            if preflight.gate is not None:
                return WorkerResult[CloneOutput](
                    status="failed",
                    error=WorkerError(
                        failure_class=FailureClass.PREFLIGHT,
                        retryable=False,
                        stderr_tail=redact_text(preflight.gate),
                    ),
                    completed_units=completed,
                )

            cut_worktree = "worktree" in owed and preflight.head_sha is not None
            if cut_worktree:
                if ctx.expired(loop_now()) or ctx.cancelled():
                    return self._interrupted(completed, owed)
                await self._materialize_worktree(ctx, mirror, worktree, preflight)
                completed.append("worktree")
        except (GitCommandError, GitError, OSError) as exc:
            return WorkerResult[CloneOutput](
                status="failed",
                error=self._error_for(exc),
                completed_units=completed,
            )

        return WorkerResult[CloneOutput](
            status="ok",
            output=CloneOutput(
                repo_id=payload.repo_id,
                url=clean_url,
                mirror_path=str(mirror),
                worktree_path=str(worktree) if preflight.head_sha is not None else None,
                default_branch=preflight.default_branch,
                default_branch_source=preflight.default_branch_source,
                head_sha=preflight.head_sha,
                commit_count=preflight.commit_count,
                size_bytes=preflight.size_bytes,
                is_shallow=preflight.is_shallow,
                submodule_count=preflight.submodule_count,
                has_lfs=preflight.has_lfs,
                largest_blob_bytes=preflight.largest_blob_bytes,
                preflight_ok=preflight.head_sha is not None,
                findings=preflight.findings,
            ),
            completed_units=[unit for unit in UNITS if unit in set(completed)],
            evidence=[str(mirror)],
        )

    async def on_cancel(self, ctx: WorkerContext) -> None:
        """Delete the debris a killed `git worktree add` leaves at `ctx.workdir`, and nothing else.

        A directory that exists but holds no `.git` entry is a half-created worktree: the next
        attempt's `git worktree add` would refuse it ("already exists") and the repo would be
        stuck on a directory nobody owns. A *valid* worktree is deliberately left alone — it is
        exactly what the next attempt is supposed to reuse.
        """
        await asyncio.to_thread(_remove_half_created_worktree, Path(ctx.workdir))

    # -- units ---------------------------------------------------------------------------

    async def _materialize_mirror(
        self, ctx: WorkerContext, payload: CloneInput, mirror: Path, clean_url: str
    ) -> None:
        """Clone the mirror, or refresh the one that is already there (§3.1 step 1).

        The clone is the ONLY place the credential-bearing URL is used, and `origin` is rewritten
        to the stripped form immediately afterwards, so every later probe — and every error any
        of them raises — can only ever see a credential-free remote.
        """
        exists = await asyncio.to_thread(_mirror_is_initialized, mirror)
        async with ctx.limits.git_net:
            if exists:
                await self._git(mirror, ctx).exec(["remote", "update", "--prune"])
                ctx.log.info("mirror_refreshed", url=clean_url, mirror=str(mirror))
                return
            await asyncio.to_thread(Path(payload.cache_dir).mkdir, parents=True, exist_ok=True)
            await self._git(Path(payload.cache_dir), ctx).exec(
                ["clone", "--mirror", payload.url, str(mirror)]
            )
        await self._git(mirror, ctx).exec(["remote", "set-url", "origin", clean_url])
        # `clean_url`, never `payload.url`: this line is the one place a token would otherwise
        # reach the log pipeline, and §11.4's redactor is the second defence, not the first.
        ctx.log.info("mirror_cloned", url=clean_url, mirror=str(mirror))

    async def _materialize_worktree(
        self, ctx: WorkerContext, mirror: Path, worktree: Path, preflight: _Preflight
    ) -> None:
        """Cut (or re-use) the detached worktree §3.1 step 1 hands to steps 2–4."""
        if await asyncio.to_thread(_worktree_is_initialized, worktree):
            return
        await asyncio.to_thread(_remove_half_created_worktree, worktree)
        git = self._git(mirror, ctx)
        await git.exec(["worktree", "prune"])
        await asyncio.to_thread(worktree.parent.mkdir, parents=True, exist_ok=True)
        await git.exec(["worktree", "add", "--detach", str(worktree), str(preflight.head_sha)])
        ctx.log.info("worktree_cut", worktree=str(worktree), head_sha=preflight.head_sha)

    # -- preflight -----------------------------------------------------------------------

    async def _preflight(
        self, ctx: WorkerContext, payload: CloneInput, mirror: Path
    ) -> _Preflight:
        """Every probe in §3.1 step 1's table, in one pass over the mirror."""
        git = self._git(mirror, ctx)
        findings: list[str] = []

        branch, source = await self._default_branch(git, payload)
        commit_count = _int_or_zero(await git.text(["rev-list", "--count", "--all"]))
        head_sha = await self._resolve_head(git, branch) if branch else None
        if commit_count == 0 or head_sha is None:
            findings.append("EmptyRepo")
            return _Preflight(
                default_branch=branch,
                default_branch_source=source,
                head_sha=None,
                commit_count=commit_count,
                size_bytes=await asyncio.to_thread(_dir_size, mirror),
                is_shallow=False,
                submodule_count=0,
                has_lfs=False,
                largest_blob_bytes=0,
                findings=tuple(findings),
            )

        is_shallow = await asyncio.to_thread(_is_shallow, mirror)
        gate: str | None = None
        if is_shallow:
            gate = await self._unshallow(ctx, git, payload)
            is_shallow = await asyncio.to_thread(_is_shallow, mirror)
            if gate is None and is_shallow:
                # The fetch reported success and the mirror is STILL shallow: the remote served
                # everything it is ever going to serve. THAT is the repo's own shape, identical on
                # every attempt, so it is the one shallow outcome that is honestly a non-retryable
                # `PREFLIGHT` gate. Keeping it is the point of moving the *transient* fetch
                # failures out of here: the label stops being a lie without becoming unreachable.
                #
                # The reason is NOT that `git-filter-repo` refuses a shallow repository — it does
                # not. Upstream contains no shallow check and no refusal path at all, and
                # `rewrite.relocate()` passes `--force` regardless, which bypasses the freshness
                # check that *does* exist. Nothing would stop the rewrite; that is precisely the
                # problem. `fast-export`/`fast-import` do not carry the shallow boundary, so
                # rewriting a shallow mirror imports a SILENTLY TRUNCATED history into the
                # monorepo — a package whose history simply stops, with no error anywhere to say
                # so. A refusal would at least be loud. This gate is what makes it loud.
                gate = (
                    "mirror is still shallow after a successful `git fetch --unshallow`; "
                    "rewriting it would import a silently truncated history (§3.1 step 1)"
                )

        submodules = await self._submodule_count(git, head_sha)
        if submodules:
            findings.append("SubmodulePresent")

        has_lfs = await self._has_lfs(git, head_sha)
        if gate is None and has_lfs and payload.require_lfs_binary and not shutil.which("git-lfs"):
            gate = "git-lfs is not on PATH but the repo declares `filter=lfs` (§3.1 step 1)"

        largest_blob = await self._largest_blob_bytes(ctx, mirror)
        if largest_blob > payload.max_blob_bytes:
            findings.append("OversizeBlob")

        size_bytes = await asyncio.to_thread(_dir_size, mirror)
        if gate is None and size_bytes > payload.max_repo_bytes:
            gate = (
                f"mirror is {size_bytes} bytes, over preflight.max_repo_bytes="
                f"{payload.max_repo_bytes}: this repo is migrated by an operator (§3.1 step 1)"
            )

        return _Preflight(
            default_branch=branch,
            default_branch_source=source,
            head_sha=head_sha,
            commit_count=commit_count,
            size_bytes=size_bytes,
            is_shallow=is_shallow,
            submodule_count=submodules,
            has_lfs=has_lfs,
            largest_blob_bytes=largest_blob,
            findings=tuple(findings),
            gate=gate,
        )

    async def _default_branch(self, git: Git, payload: CloneInput) -> tuple[str, str]:
        """`symbolic-ref` → the configured fallbacks → the first `refs/heads/*`, in that order."""
        head = await git.exec(["symbolic-ref", "--short", "HEAD"], check=False)
        name = head.stdout_tail.strip()
        if head.ok and name:
            return name, "symbolic-ref"
        for candidate in payload.branch_fallbacks:
            if await git.ref_exists(f"refs/heads/{candidate}"):
                return candidate, "fallback"
        refs = await git.list_refs("refs/heads/")
        if refs:
            return sorted(refs)[0].removeprefix("refs/heads/"), "fallback"
        return "", "fallback"

    async def _resolve_head(self, git: Git, branch: str) -> str | None:
        """`branch`'s commit, or `None` **only** when the branch genuinely does not resolve.

        Deliberately not `Git.resolve`. That is `rev-parse --verify --quiet` with `check=False`
        returning `sha if result.ok and sha else None`, which is right for its own contract ("has
        this run's anchor been created yet?") and wrong here: it makes a rev-parse that was never
        started, or was killed at the deadline, indistinguishable from "this repo has no commits".

        Feeding that `None` into the `EmptyRepo` branch below is the worst outcome this module
        can produce — worse than a wrong non-retryable failure. The worker would return
        `status="ok"` with `head_sha=None`, persist a durable `EmptyRepo` finding about a repo
        that has commits, cut no worktree, and every later worker would then report "worktree
        does not exist; run the clone worker first". Both ends of that operator story are wrong,
        and because the verdict was reported as SUCCESS nothing downstream ever re-asks.

        So a probe with no verdict raises (retryable), and only a rev-parse that actually ran and
        actually said "no such rev" is allowed to mean an empty repo.
        """
        result = await git.exec(
            ["rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}"], check=False
        )
        reason = _no_verdict(result)
        if reason is not None:
            raise _indeterminate(result, f"resolving {branch!r} produced no answer — {reason}",
                                 cwd=git.path)
        sha = result.stdout_tail.strip()
        return sha if result.ok and sha else None

    async def _unshallow(self, ctx: WorkerContext, git: Git, payload: CloneInput) -> str | None:
        """Auto-remediate a shallow mirror. A gate here is PERMANENT, so only a *settled* answer
        may produce one.

        A gate returns non-retryable `FailureClass.PREFLIGHT`, which `RetryPolicy` sends straight
        to `REQUIRES_HUMAN_INTERVENTION` without charging an attempt — and `PREFLIGHT` is defined
        as "the repo's own shape; identical on every attempt". But this is the single most
        transient call the worker makes: it holds `ctx.limits.git_net` and talks to a remote. A
        TCP reset, an expired token, a deadline that passed before the call (`started=False`,
        exit 124) and a `DEFAULT_TIMEOUT_S` kill (a negative signal code) are all failures to
        *find out* whether this mirror can be unshallowed. None of them is a property of the
        repo, and labelling any of them `PREFLIGHT` turns a network blip into an immutable
        verdict a human has to clear by hand.

        So every failure of the fetch itself is raised and classified by `_error_for` — the same
        `TIMEOUT` / `TRANSIENT_INFRA`, retryable treatment the `clone` and `remote update` on
        this very remote already get twelve lines above. Two answers ARE settled and stay a
        non-retryable gate: the operator disabled unshallowing, and (in `_preflight`) a fetch
        that *succeeded* and left the mirror shallow anyway.
        """
        if not payload.unshallow:
            return "mirror is shallow and preflight.unshallow is disabled (§3.1 step 1)"
        async with ctx.limits.git_net:
            result = await git.exec(["fetch", "--unshallow"], check=False)
        if result.ok:
            return None
        reason = _no_verdict(result) or (
            f"`git fetch --unshallow` failed (exit {result.exit_code})"
        )
        raise _indeterminate(result, f"mirror is shallow and {reason}", cwd=git.path)

    async def _submodule_count(self, git: Git, head_sha: str) -> int:
        """How many submodules `.gitmodules` declares at `head_sha`.

        `git show` exits non-zero for a path that is not in the tree, and THAT non-zero exit is
        the legitimate zero — most repos have no `.gitmodules`. A `show` that never started or
        was killed is not: returning `0` for it writes `repos.submodule_count = 0` as a
        measurement, drops the `SubmodulePresent` finding, and — because the worker still returns
        `ok` — leaves nothing to say the number was never taken. An unmeasured value is not a
        measurement of zero, so it is raised (retryable) rather than fabricated.
        """
        result = await git.exec(["show", f"{head_sha}:.gitmodules"], check=False)
        reason = _no_verdict(result)
        if reason is not None:
            raise _indeterminate(result, f"the submodule probe produced no answer — {reason}",
                                 cwd=git.path)
        if not result.ok:
            return 0
        return sum(1 for _ in _iter_submodule_names(result.stdout_tail))

    async def _has_lfs(self, git: Git, head_sha: str) -> bool:
        """Does `.gitattributes` at `head_sha` declare `filter=lfs`?

        The same shape as `_submodule_count`, with a sharper consequence: `False` here DISARMS
        the `git-lfs`-on-PATH gate two lines below its caller. A probe that never ran would
        therefore let a repo whose checkout needs `git-lfs` past a gate built to stop exactly
        that, and report `has_lfs=False` as a fact while doing it. "We could not look" is not
        "there is no LFS", so it raises instead.
        """
        result = await git.exec(["show", f"{head_sha}:.gitattributes"], check=False)
        reason = _no_verdict(result)
        if reason is not None:
            raise _indeterminate(result, f"the LFS probe produced no answer — {reason}",
                                 cwd=git.path)
        return result.ok and "filter=lfs" in result.stdout_tail

    async def _largest_blob_bytes(self, ctx: WorkerContext, mirror: Path) -> int:
        """`cat-file --batch-all-objects`, read back off disk one line at a time.

        This one probe calls `util.proc.run` directly rather than the injected `CommandRunner`:
        `log_dir` is not part of that Protocol, and the tail-only capture every other call uses
        would silently answer "the largest blob among the last 32 KiB of output".

        Nothing here has a legitimate non-zero exit: `cat-file --batch-all-objects` is a local
        walk of a mirror that has already been proven to hold commits. So every non-ok result is
        a scan that did not happen, and `0` — the value the caller compares against
        `max_blob_bytes` — would be a *measured* number reported for a measurement never taken,
        silencing `OversizeBlob` while the worker returned `ok`. It raises instead.
        """
        with scoped_tempdir(prefix="fleet-blobscan-") as sink:
            result = await proc_run(
                ["git", "-C", str(mirror), "cat-file", "--batch-all-objects", "--batch-check"],
                cwd=mirror,
                deadline=ctx.deadline,
                log_dir=sink,
                log_stem="cat-file",
            )
            reason = _no_verdict(result)
            if reason is None and not result.ok:
                reason = f"the blob scan failed (exit {result.exit_code})"
            if reason is None and result.stdout_path is None:  # pragma: no cover - log_dir given
                reason = "the blob scan captured no output file"
            if reason is not None or result.stdout_path is None:
                raise _indeterminate(result, reason or "the blob scan produced no answer",
                                     cwd=mirror)
            return await asyncio.to_thread(_largest_blob, result.stdout_path)

    # -- helpers -------------------------------------------------------------------------

    def _mirror_path(self, payload: CloneInput) -> Path:
        return Path(payload.cache_dir) / f"{slug(payload.repo_id)}.git"

    def _worktree_path(self, ctx: WorkerContext, payload: CloneInput) -> Path:
        return Path(payload.worktree_path or ctx.workdir)

    def _git(self, path: Path, ctx: WorkerContext) -> Git:
        """A `Git` bound to `path` under the worker's deadline — §7.1's "every subprocess call"."""
        return Git(path, runner=self._runner, deadline=ctx.deadline)

    def _interrupted(
        self, completed: Sequence[str], owed: Sequence[str]
    ) -> WorkerResult[CloneOutput]:
        """Stopped between units. `partial` when something landed, so re-entry resumes at the
        rest; a bare `cancelled` when nothing did, because `partial` with nothing completed is
        `failed` wearing a friendlier name."""
        done = [unit for unit in UNITS if unit in set(completed)]
        remaining = [unit for unit in owed if unit not in set(done)]
        if not done:
            return WorkerResult[CloneOutput](
                status="cancelled",
                error=WorkerError(
                    failure_class=FailureClass.TIMEOUT,
                    retryable=True,
                    stderr_tail="cancelled before any unit landed",
                ),
            )
        return WorkerResult[CloneOutput](
            status="partial",
            completed_units=done,
            remaining_units=remaining,
        )

    def _error_for(self, exc: BaseException) -> WorkerError:
        """A git/OS failure as structured evidence. The tail is redacted a second time on the way
        in: `GitCommandError` already scrubs its own argv and stderr, and an `OSError` message can
        name a path built from the URL.

        `DiskFloorBreached` is an `OSError` and would otherwise be reported as transient infra and
        retried — three more attempts at filling a volume that is already too full. It is
        `DISK_EXHAUSTED` and not retryable: the run halts on it (exit 9, §11.3) because the next
        repo would meet exactly the same floor.
        """
        if isinstance(exc, DiskFloorBreached):
            return WorkerError(
                failure_class=FailureClass.DISK_EXHAUSTED,
                retryable=False,
                stderr_tail=redact_text(str(exc)),
                exception_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
            )
        exit_code = getattr(exc, "exit_code", None)
        # `started` and `timed_out` are read TOGETHER and handed to the one function that owns the
        # distinction (`base.clock_failure`), which `buildverify.classify_build_failure` also
        # calls. Reading `timed_out` alone here is the defect this replaces — but the two
        # classifiers did not diverge because of it until `44d5550` MADE them: before that commit
        # both read `timed_out` alone and both answered `TIMEOUT` for a probe that never ran (per
        # `git show 44d5550~1`), so they agreed, wrongly, in step. `44d5550` reordered only
        # `buildverify.classify_build_failure`'s branches to check `started` first — fixing that
        # module alone — and added a comment claiming clone already drew the same line "for this
        # reason; the two are meant to stay in step". This file was untouched by that commit, so
        # from `44d5550` to `68a41ff` the two genuinely disagreed (buildverify `TRANSIENT_INFRA`,
        # clone `TIMEOUT`) for a shape neither had ever produced before — a divergence manufactured
        # by that half-applied reorder, not inherited from any pre-existing mismatch. It existed
        # only in this dev tree, between those two checkpoints, and no code that ran a real wave
        # ever saw it. An `OSError` carries neither attribute; `started=True, timed_out=False` is
        # right for it (the syscall did happen) and falls through to the same `TRANSIENT_INFRA`
        # this branch has always produced.
        started = bool(getattr(exc, "started", True))
        timed_out = bool(getattr(exc, "timed_out", False))
        failure_class, _ = clock_failure(started=started, timed_out=timed_out) or (
            FailureClass.TRANSIENT_INFRA,
            True,
        )
        return WorkerError(
            failure_class=failure_class,
            retryable=True,
            exit_code=exit_code if isinstance(exit_code, int) else None,
            stderr_tail=redact_text(str(exc)),
            exception_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
        )


def _is_shallow(mirror: Path) -> bool:
    """Does `mirror` have a shallow boundary — i.e. does `.git/shallow` name at least one root?

    Content-aware rather than `(mirror / "shallow").exists()`, because the two are not the same
    question. `shallow` is a list of grafted commit ids, and a file holding none of them (empty,
    or whitespace only) declares no boundary: the repository is complete. Presence alone would
    read such a file as "shallow", and since a shallow verdict that survives a successful
    `--unshallow` is a non-retryable `PREFLIGHT` gate, a stray empty file is enough to send a
    perfectly complete repo to a human.

    **Reading the file is a NARROWER signal than `git rev-parse --is-shallow-repository`,
    deliberately.** That predicate reports `true` for a planted zero-byte `shallow` on an
    otherwise-complete repo — measured against this host's git (2.43.0, per
    `docs/INTEGRATION_HONESTY.md`) and pinned by
    `test_a_shallow_file_that_declares_no_boundary_is_not_a_shallow_repository`, which invokes the
    real binary — so spending a subprocess on the predicate would buy the identical blind spot
    this function does not have. That a successful `fetch --unshallow` removes the file is
    standard, documented git behaviour; this repo has not swept it across versions, so no version
    range is claimed here — only the one blind spot actually measured above.

    A missing file, and a `shallow` that is a directory or is otherwise unreadable, both answer
    "no boundary declared": absence of evidence for a boundary is what "not shallow" means here,
    and this returns rather than raises so it cannot be confused with a probe that got no answer.
    """
    try:
        text = (mirror / "shallow").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(line.strip() for line in text.splitlines())


def _mirror_is_initialized(mirror: Path) -> bool:
    """A directory git would recognise as a bare repository — not merely a directory that exists.
    A killed clone leaves the latter, and treating it as a mirror is how a repo gets stuck."""
    return (mirror / "HEAD").is_file() and (mirror / "objects").is_dir()


def _worktree_is_initialized(worktree: Path) -> bool:
    """A linked worktree carries a `.git` FILE pointing at the mirror's admin directory."""
    return (worktree / ".git").exists()


def _remove_half_created_worktree(worktree: Path) -> None:
    """Remove `worktree` only when it is debris: it exists and has no `.git` entry at all."""
    if worktree.exists() and not _worktree_is_initialized(worktree):
        shutil.rmtree(worktree, ignore_errors=True)


def _int_or_zero(text: str) -> int:
    """`rev-list --count` on a repo with no refs prints nothing; that is 0 commits, not a crash."""
    try:
        return int(text.strip())
    except ValueError:
        return 0
