"""History-rewriting ingest into the monorepo layout (SPEC §3.3 step 1, ADR-0011).

Phase 3 does not copy a tree into the monorepo; it rewrites *history* with `git-filter-repo` so
`git log --follow` still answers questions about a file after it has moved, and then merges that
rewritten history in with `--allow-unrelated-histories`. Provenance rides on the merge commit as
`Source-Repo:` / `Source-Sha:` trailers (plus `Hoisted-Contract:` for a hoisted contract node), so
the origin of a file that no longer lives in the repo that wrote it is recorded *in git* rather
than only in SQLite.

**Offline by construction.** Nothing here contacts a remote. `git-filter-repo` runs against a
throwaway local clone, and the history it produced is brought into the integration repo by
`git fetch <filesystem-path>` — a local-path fetch, which is why a `--network=none` build of this
step is possible at all. There is no `--mirror`, no `push`, and no URL anywhere in this module.

**Re-runnable.** Two mechanisms, because a Phase 3 task is retried up to three times (ADR-0014):
`relocate()` always passes `--force` (git-filter-repo otherwise refuses any repo it considers
non-fresh, which every retry's clone is), and `ingest()` first asks git whether a merge carrying
this `Source-Repo`/`Source-Sha` pair is already on the integration branch and, if so, takes a fresh
snapshot instead of merging twice.

**Single writer, immutable reads.** The integration branch is the one mutable resource every repo
in a wave wants to write, so per-worktree exclusion buys nothing: `IntegrationMutex` is a `flock`
held across the whole filter → fetch → merge → `update-ref` sequence, and merges land one node at a
time. Concurrent `--allow-unrelated-histories` merges racing one ref update orphan all but one
merge commit, leaving an `attempts.commit_sha` unreachable from `integration` — a corruption no
later phase can detect. On release, the writer has already created
`refs/fleet/<run_id>/integration/<seq>` at the post-merge tip: **every build reads that immutable
snapshot, never the branch**, so a 20-minute `bazel build` cannot see merges landing mid-build and
a `BUILD_ERROR` is a property of a named tree rather than of scheduling luck.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Final
from uuid import UUID

from fleet.sandbox.worktree import slug
from fleet.util.proc import CommandRunner, run
from fleet.vcs.git import Git, GitError

__all__ = [
    "DEFAULT_FILTER_REPO_BIN",
    "SOURCE_REPO_TRAILER",
    "SOURCE_SHA_TRAILER",
    "FilterRepoUnavailableError",
    "HistoryScrubUnavailableError",
    "IngestError",
    "IngestResult",
    "IntegrationMutex",
    "LockTimeoutError",
    "RelocationSpec",
    "SnapshotRef",
    "SourceProvenance",
    "filter_repo_argv",
    "ingest",
    "integration_snapshot",
    "merge_source",
    "relocate",
    "resolve_replace_text",
]

DEFAULT_FILTER_REPO_BIN: Final = "git-filter-repo"
SOURCE_REPO_TRAILER: Final = "Source-Repo"
SOURCE_SHA_TRAILER: Final = "Source-Sha"
HOISTED_CONTRACT_TRAILER: Final = "Hoisted-Contract"
DEFAULT_BLOB_LIMIT: Final = "10M"

_SEQ_RE: Final = re.compile(r"/(\d+)$")


class FilterRepoUnavailableError(GitError):
    """`git-filter-repo` is not on PATH. A hard, named failure rather than a silent fallback to
    `git filter-branch`, which would rewrite history with different (and slower) semantics."""


class HistoryScrubUnavailableError(GitError):
    """`redaction.history_scrub_file` (§11.4) names a path that does not exist on disk.

    Deliberately a different type from `FilterRepoUnavailableError`: that one means the
    `git-filter-repo` binary itself is missing. This one means the binary is fine but the
    `--replace-text` scrub LIST it would be handed is not there — a typo in
    `history_scrub_file`, or an un-provisioned `config/` directory. Collapsing the two into one
    message is the exact "four-state collapse" shape this codebase's own audit (D34-D45) warns
    about: a caller catching one must not silently also catch the other.
    """


class IngestError(GitError):
    """The ingest sequence refused to proceed — wrong branch checked out, missing source ref, or a
    merge that failed. Never downgraded to a warning (Rule 11)."""


class LockTimeoutError(GitError):
    """The integration mutex could not be acquired inside the caller's bound. The wave scheduler
    treats this as `TRANSIENT_INFRA`; it is never a reason to merge without the lock."""


@dataclass(frozen=True, slots=True)
class RelocationSpec:
    """What Phase 2's relocation plan means to `git-filter-repo`, as data.

    *Agent recommendation* (CLAUDE.md guardrail 1): the spec names a `RelocationPlan` model that
    does not exist in `models/` yet, so this module declares the minimum shape it needs and the
    plan model can be adapted onto it later. It is deliberately not a pydantic model — the argv
    builder must stay callable from a test with two strings.
    """

    dest_path: str
    """Where the tree lands in the monorepo, e.g. `ts/@acme/billing`."""

    source_paths: tuple[str, ...] = ()
    """Paths to KEEP. Empty means the whole repo. Non-empty is the hoisted-contract case (§3.3):
    only the commits that touched the contract's sources survive the rewrite."""

    source_prefix: str = ""
    """The prefix rewritten to `dest_path`. Empty rewrites the repo root (`--path-rename ':dest/'`),
    which is the ordinary whole-repo relocation."""

    strip_blobs_bigger_than: str | None = DEFAULT_BLOB_LIMIT
    """§3.2 step 1: oversized blobs never enter the monorepo. None disables the filter."""

    replace_text: Path | None = None
    """`config/rules/secrets.txt` (§11.4) — secret scrub applied to history, not just to logs."""

    extra_args: tuple[str, ...] = ()


def filter_repo_argv(
    spec: RelocationSpec, *, binary: str = DEFAULT_FILTER_REPO_BIN, force: bool = True
) -> tuple[str, ...]:
    """Build the `git-filter-repo` argv. Pure, so the command is assertable without the binary.

    `--force` is unconditional by default: every retry of a Phase 3 task filters a *fresh throwaway
    clone*, but git-filter-repo's freshness heuristic (a clone with a reflog, or one already
    filtered) refuses often enough that the retry would fail for a reason unrelated to the
    migration. The re-run safety that matters is `ingest()`'s merge-idempotency check, not this.
    """
    argv: list[str] = [binary]
    for path in spec.source_paths:
        argv += ["--path", path]
    argv += ["--path-rename", f"{spec.source_prefix}:{spec.dest_path.rstrip('/')}/"]
    if spec.strip_blobs_bigger_than:
        argv += ["--strip-blobs-bigger-than", spec.strip_blobs_bigger_than]
    if spec.replace_text is not None:
        argv += ["--replace-text", str(spec.replace_text)]
    if force:
        argv.append("--force")
    argv += list(spec.extra_args)
    return tuple(argv)


def resolve_replace_text(root: Path, configured: str) -> Path | None:
    """Resolve `redaction.history_scrub_file` into `RelocationSpec.replace_text` (§11.4, D21).

    Mirrors `cli._forge_token_config`'s resolution of the Gitea credential path — both are
    file-shaped settings resolved against `FleetSettings.root` — so the two file-shaped settings
    in this codebase behave the same way instead of diverging by accident.

    An empty/blank `configured` disables the file-based scrub explicitly: the caller wanted no
    `--replace-text` and says so, rather than the setting silently doing nothing the way it did
    before any caller read it at all (D21: *"the setting that would feed it… is read by
    nothing"*). A non-empty `configured` naming a path that is not there is refused loudly
    (Rule 11) rather than resolved to `None`: D21's own severity note is that a scrub which is
    "implemented but never invoked means secrets that were supposed to be redacted ship into the
    monorepo" — silently dropping a typo'd or un-provisioned path would reproduce exactly that.
    """
    cleaned = configured.strip()
    if not cleaned:
        return None
    path = (root / cleaned).resolve()
    if not path.is_file():
        raise HistoryScrubUnavailableError(
            f"redaction.history_scrub_file={cleaned!r} does not exist at {path}; §11.4 promises "
            "secrets are scrubbed out of rewritten history, and proceeding without this file "
            "would pass no --replace-text at all — the exact silent gap this function closes"
        )
    return path


async def relocate(
    clone_dir: Path,
    spec: RelocationSpec,
    *,
    runner: CommandRunner = run,
    binary: str = DEFAULT_FILTER_REPO_BIN,
    deadline: float | None = None,
    timeout_s: float | None = 1800.0,
) -> None:
    """Rewrite `clone_dir`'s history into the monorepo layout, in place.

    `clone_dir` must be a THROWAWAY clone: git-filter-repo rewrites every commit and drops the
    origin remote, so pointing this at a mirror would destroy the mirror. Nothing here fetches.
    """
    argv = filter_repo_argv(spec, binary=binary)
    # No shell is ever involved (`util/proc.py`'s module docstring), so `git-filter-repo` missing
    # from PATH never comes back as a `ProcResult` with some sentinel exit code or an "stderr" —
    # there is no shell to apply the "command not found: exit 127" convention, and the process
    # never started well enough to write anything. It is `asyncio.create_subprocess_exec` raising
    # `FileNotFoundError` in THIS process, before any `ProcResult` exists, and it is caught here
    # or it escapes raw and unclassified — which is what `not result.started or exit_code == 127
    # or "No such file" in result.stderr_tail` used to guess at from a `ProcResult` that can
    # never actually carry that evidence.
    try:
        result = await runner(argv, cwd=clone_dir, deadline=deadline, timeout_s=timeout_s)
    except FileNotFoundError as exc:
        raise FilterRepoUnavailableError(
            f"{binary} is not on PATH: Phase 3 ingest rewrites history and has no fallback; "
            f"install git-filter-repo on the host or in the sandbox image: {exc}"
        ) from exc
    # `result.started` is false in exactly one case — `util.proc.run` synthesised a deadline that
    # had already passed (§7.1) — never a missing binary; that case already fails `result.ok`
    # below and is reported as the clock failure it is, via `IngestError`.
    if not result.ok:
        raise IngestError(
            f"{binary} failed (exit {result.exit_code}) in {clone_dir}: {result.stderr_tail}"
        )


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    """What the merge commit records about where this history came from (ADR-0011/ADR-0019)."""

    repo_id: str
    sha: str
    contract_id: str | None = None

    def trailers(self) -> dict[str, str]:
        out = {SOURCE_REPO_TRAILER: self.repo_id, SOURCE_SHA_TRAILER: self.sha}
        if self.contract_id is not None:
            out[HOISTED_CONTRACT_TRAILER] = self.contract_id
        return out


@dataclass(frozen=True, slots=True)
class SnapshotRef:
    """The immutable ref a build actually runs against — `attempts.integration_ref` (§3.3)."""

    ref: str
    sha: str
    seq: int


@dataclass(frozen=True, slots=True)
class IngestResult:
    """One node's ingest. `already_present` marks the idempotent path: the merge was found on the
    branch, so nothing was merged twice, and only a fresh snapshot was cut."""

    merge_sha: str
    snapshot: SnapshotRef
    already_present: bool
    source: SourceProvenance = field(default_factory=lambda: SourceProvenance("", ""))


class IntegrationMutex:
    """`flock` on the integration repo: the single-writer merge queue of SPEC §3.3 step 1.

    Non-blocking `flock` in a poll loop rather than a blocking one in a thread: a blocking
    `flock(LOCK_EX)` inside the event loop stalls every other coroutine in the process, including
    the heartbeats that keep this run's leases alive.

    The lock is advisory and per-file, so it also excludes a *second `fleet` process* — which is the
    case that matters, since twenty Phase 3 tasks may be in flight and `fleet resume` may be
    starting alongside them.
    """

    def __init__(
        self,
        lock_dir: Path | str,
        run_id: UUID | str,
        *,
        poll_interval_s: float = 0.1,
        timeout_s: float | None = 900.0,
    ) -> None:
        self.lock_path = Path(lock_dir) / f"fleet-integration-{slug(str(run_id))}.lock"
        self.poll_interval_s = poll_interval_s
        self.timeout_s = timeout_s
        self._fd: int | None = None

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        expiry = None if self.timeout_s is None else loop.time() + self.timeout_s
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if expiry is not None and loop.time() >= expiry:
                    os.close(fd)
                    raise LockTimeoutError(
                        f"integration mutex {self.lock_path} held by another writer for longer "
                        f"than {self.timeout_s}s; refusing to merge without it (§3.3 step 1)"
                    ) from None
                await asyncio.sleep(self.poll_interval_s)
            else:
                self._fd = fd
                return

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    @property
    def held(self) -> bool:
        return self._fd is not None

    async def __aenter__(self) -> IntegrationMutex:
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def snapshot_prefix(run_id: UUID | str) -> str:
    return f"refs/fleet/{slug(str(run_id))}/integration"


async def integration_snapshot(git: Git, run_id: UUID | str, *, tip: str = "HEAD") -> SnapshotRef:
    """Cut `refs/fleet/<run_id>/integration/<seq>` at `tip` and return it.

    `seq` is derived from the refs git already holds, not from a counter in SQLite: a crash between
    "increment the counter" and "create the ref" would otherwise silently reuse a snapshot name and
    make two different trees answer to one `attempts.integration_ref`.
    """
    prefix = snapshot_prefix(run_id)
    highest = -1
    for ref in await git.list_refs(prefix):
        match = _SEQ_RE.search(ref)
        if match:
            highest = max(highest, int(match.group(1)))
    seq = highest + 1
    sha = await git.rev_parse(tip)
    ref = f"{prefix}/{seq}"
    await git.update_ref(ref, sha, message=f"fleet integration snapshot {seq}")
    return SnapshotRef(ref=ref, sha=sha, seq=seq)


async def already_ingested(git: Git, branch: str, source: SourceProvenance) -> str | None:
    """SHA of an existing merge carrying this `Source-Repo`/`Source-Sha` pair, or None.

    Both trailers must match: a repo re-ingested at a *different* origin SHA is new work, and a
    different repo that happens to share a SHA (a fork) is not this node.
    """
    for commit in await git.log(
        branch, trailer_keys=[SOURCE_REPO_TRAILER, SOURCE_SHA_TRAILER, HOISTED_CONTRACT_TRAILER]
    ):
        repo_ok = source.repo_id in commit.trailers.get(SOURCE_REPO_TRAILER, ())
        sha_ok = source.sha in commit.trailers.get(SOURCE_SHA_TRAILER, ())
        contract = commit.trailer(HOISTED_CONTRACT_TRAILER)
        if repo_ok and sha_ok and contract == source.contract_id:
            return commit.sha
    return None


def merge_message(source: SourceProvenance, *, subject: str | None = None) -> str:
    """Merge subject plus the ADR-0011 trailer block.

    Built as message text because `git merge` has no `--trailer` flag (unlike `git commit`); the
    block is still a real trailer block, so `%(trailers:key=Source-Sha)` parses it and the
    idempotency check above reads it with git's own parser rather than a regex.
    """
    head = subject or f"Merge {source.repo_id} into the monorepo"
    trailers = "\n".join(f"{key}: {value}" for key, value in source.trailers().items())
    return f"{head}\n\n{trailers}\n"


async def fetch_local(git: Git, source_dir: Path, *, source_ref: str, dest_ref: str) -> str:
    """Bring a local repository's history in as `dest_ref`. Filesystem path only — no network.

    Forced (`+`) so a retry that re-filters the same repo overwrites the previous incoming ref
    instead of failing on a non-fast-forward, which is the whole point of the rewrite being
    reproducible from the plan.
    """
    await git.exec(
        ["fetch", "--no-tags", str(source_dir), f"+{source_ref}:{dest_ref}"],
        timeout_s=1800.0,
    )
    return await git.rev_parse(dest_ref)


async def merge_source(
    git: Git,
    *,
    rev: str,
    source: SourceProvenance,
    subject: str | None = None,
) -> str:
    """`git merge --allow-unrelated-histories --no-ff` with the provenance trailers.

    A crashed merge is recovered here, not in SQLite: if `MERGE_HEAD` exists the previous attempt
    died mid-merge, and `git merge --abort` is the Git-local repair (§3.2 step 6).
    """
    if await git.merge_in_progress():
        await git.exec(["merge", "--abort"], check=False)
    await git.exec(
        [
            "merge",
            "--allow-unrelated-histories",
            "--no-ff",
            "--no-verify",
            "-m",
            merge_message(source, subject=subject),
            rev,
        ],
        with_identity=True,
    )
    return await git.rev_parse("HEAD")


async def ingest(
    git: Git,
    *,
    source_dir: Path,
    source: SourceProvenance,
    run_id: UUID | str,
    integration_branch: str = "integration",
    source_ref: str = "HEAD",
    mutex: IntegrationMutex | None = None,
) -> IngestResult:
    """The whole §3.3 step 1 sequence for ONE node, under the integration mutex.

    `git` must be a worktree with `integration_branch` checked out (a bare repo cannot merge);
    `source_dir` is the already-filtered throwaway clone. Order is fixed and the mutex spans all of
    it: idempotency check → local fetch → merge → snapshot. The snapshot is cut *before* the lock
    is released so the tip a build was promised cannot have moved by the time it reads it.
    """
    current = await git.current_branch()
    if current != integration_branch:
        raise IngestError(
            f"ingest must run on {integration_branch!r}, but {git.path} has "
            f"{current or 'a detached HEAD'} checked out; merging elsewhere would strand the "
            "merge commit off the integration branch"
        )
    lock = mutex or IntegrationMutex(await _common_dir(git), run_id)
    async with lock:
        existing = await already_ingested(git, integration_branch, source)
        if existing is not None:
            snapshot = await integration_snapshot(git, run_id, tip=integration_branch)
            await git.create_branch(f"migrate/{source.repo_id}", existing, force=True)
            return IngestResult(
                merge_sha=existing, snapshot=snapshot, already_present=True, source=source
            )
        incoming = f"refs/fleet/{slug(str(run_id))}/incoming/{slug(source.repo_id)}"
        rev = await fetch_local(git, source_dir, source_ref=source_ref, dest_ref=incoming)
        merge_sha = await merge_source(git, rev=rev, source=source)
        snapshot = await integration_snapshot(git, run_id, tip=integration_branch)
        await git.create_branch(f"migrate/{source.repo_id}", merge_sha, force=True)
        return IngestResult(
            merge_sha=merge_sha, snapshot=snapshot, already_present=False, source=source
        )


async def _common_dir(git: Git) -> Path:
    """`.git` of the MAIN checkout — every worktree of one repo shares it, so a lock taken there
    excludes writers coming in through a different worktree."""
    out = await git.text(["rev-parse", "--path-format=absolute", "--git-common-dir"])
    return Path(out.strip())


def default_source_paths(paths: Sequence[str]) -> tuple[str, ...]:
    """Normalise plan paths for `--path`: no leading `./`, no trailing slash, deduplicated,
    order-stable. git-filter-repo matches these literally, so `./src` silently keeps nothing."""
    seen: dict[str, None] = {}
    for path in paths:
        cleaned = path.strip().removeprefix("./").rstrip("/")
        if cleaned:
            seen.setdefault(cleaned, None)
    return tuple(seen)
