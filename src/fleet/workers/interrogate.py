"""Phase 1 step 2/3: the manifest walk → `ManifestRef` + `Coordinate` (§3.1 steps 2–3, ADR-0005).

One walk, one dispatch table through `fleet.manifests.base.adapter_for`, one output contract.
A manifest that fails to parse is marked `low_confidence` and keeps its `parse_error` verbatim;
it is never dropped, because a manifest that parsed to `[]` because it was garbage is
indistinguishable downstream from one that genuinely declares nothing — and that difference is a
missing graph edge.

**The catch-all adapter is a repo-level fallback, not a per-file one.** `UnknownAdapter.matches`
returns True for *any* path (priority 10 000), so offering every file to `adapter_for` and
keeping whatever answers would make every README a manifest. A path is a manifest only when a
real adapter claims it; if the whole walk finds none, §3.1 step 2's unknown-ecosystem path
synthesizes exactly ONE `ManifestRef` at `path = "."` with `low_confidence = True` and a
`Coordinate(ecosystem=UNKNOWN, group="", name=repo_id)`, plus a `no-manifest` finding. A repo is
never silently dropped.

This module also owns the **shared scan walk** — `walk_files`, `is_ignored`, `worktree_of`,
`completed_from` — which `symbolindex.py` and `classify.py` import. §3.1 describes one walk of
the worktree honoring one `ignore_globs` list; two copies of that predicate would be two
definitions of which files the fleet can see.
"""

from __future__ import annotations

import asyncio
import re
import stat
from collections.abc import Iterable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import ClassVar, Final

from pydantic import Field

from fleet.manifests.base import ManifestAdapter, ManifestParseError, adapter_for
from fleet.models.enums import Ecosystem, FailureClass, Phase
from fleet.models.repo import Coordinate, ManifestRef, RepoId
from fleet.orchestrator.registry import register_worker
from fleet.util.hashing import sha256_bytes, sha256_file
from fleet.workers.base import (
    BaseWorker,
    WorkerContext,
    WorkerError,
    WorkerInput,
    WorkerOutput,
    WorkerResult,
    loop_now,
)

__all__ = [
    "DEFAULT_IGNORE_GLOBS",
    "UNKNOWN_MANIFEST_PATH",
    "InterrogateInput",
    "InterrogateOutput",
    "InterrogateWorker",
    "ManifestDependency",
    "checkpointed_units",
    "completed_from",
    "is_ignored",
    "owed_from",
    "paths_intact",
    "walk_files",
    "worktree_of",
    "worktree_presence",
]

DEFAULT_IGNORE_GLOBS: Final[tuple[str, ...]] = (
    "**/node_modules/**",
    "**/target/**",
    "**/build/**",
    "**/dist/**",
    "**/vendor/**",
    "**/.venv/**",
    "**/testdata/**",
    "**/*.min.js",
)
"""`scan.ignore_globs` as shipped (§9). A DEFAULT for the payload, never authority: the run's
config is what the runner puts on `InterrogateInput`."""

UNKNOWN_MANIFEST_PATH: Final = "."
"""Where §3.1 step 2's synthesized `ManifestRef` lives: the repo root, not an invented filename —
inventing one would put a path in `manifests.path` that no `ls-tree` will ever confirm."""

_UNKNOWN_ADAPTER: Final = "unknown"

_GIT_DIRS: Final = frozenset({".git"})


@lru_cache(maxsize=512)
def _glob_regex(glob: str) -> re.Pattern[str]:
    """Compile one `**`-aware glob against POSIX repo-relative paths.

    `fnmatch` treats `*` as matching `/` (so `**/x` and `*/x` become the same rule) and
    `PurePath.match` is right-anchored, so neither answers "is this file under a `node_modules`
    anywhere". Cached because the same eight globs are asked about once per file in the tree.
    """
    out: list[str] = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile(f"^{''.join(out)}$")


def is_ignored(rel_path: str, ignore_globs: Sequence[str]) -> bool:
    """Does `rel_path` (repo-relative, POSIX) match any ignore glob?"""
    return any(_glob_regex(glob).match(rel_path) for glob in ignore_globs)


def walk_files(root: Path, ignore_globs: Sequence[str]) -> list[str]:
    """Every regular file under `root`, repo-relative, POSIX, **sorted**.

    Sorted because §11.6 requires two runs over the same tree to produce the same rows in the
    same order; filesystem order is not a promise any filesystem makes. `.git` is skipped
    outright — the mirror's object store is not source, and walking it in a large repo costs more
    than the rest of the tree put together. Symlinks are not followed: a link out of the worktree
    is not this repo's content, and a link loop is not a scan the fleet ever returns from.
    """
    found: list[str] = []
    for path in root.rglob("*"):
        if any(part in _GIT_DIRS for part in path.parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if is_ignored(rel, ignore_globs):
            continue
        found.append(rel)
    return sorted(found)


def worktree_of(ctx: WorkerContext, override: str | None) -> Path:
    """The tree a scan worker reads: the payload's path, else the lease's own `ctx.workdir`."""
    return Path(override or ctx.workdir)


def worktree_presence(root: Path) -> bool | OSError:
    """Whether `root` exists as a directory — genuinely, not as `Path.is_dir()` reports it (D40).

    `Path.is_dir()` calls `os.stat` internally and turns EVERY `OSError` into a bare `False`:
    EACCES on a parent, ELOOP, a stale NFS handle, ENAMETOOLONG all read identically to "the
    clone never ran". A worker that reports `PREFLIGHT, retryable=False` off that single `False`
    cannot tell a genuinely absent worktree from a transient filesystem fault, and abandons the
    repo instead of retrying it — exactly the shape `util.proc.no_verdict` guards against for a
    `ProcResult`, re-derived here because there is no subprocess: `is_dir()`'s `False` is the
    stat-syscall analogue of a `ProcResult` with no verdict.

    Stats the path directly instead of going through `is_dir()`. `FileNotFoundError` and
    `NotADirectoryError` are the two shapes `stat` raises for a path (or a path component) that
    is genuinely not there — a SETTLED negative, worth exactly what `Path.is_dir()`'s `False`
    used to mean. Every other `OSError` means the filesystem did not answer the question at all,
    and is returned AS the exception (not swallowed) so the caller can tell "no" from "we don't
    know" and fail retryable on the latter rather than terminal.

    Returns `True` when `root` exists and is a directory, `False` for a genuine absence, or the
    `OSError` itself when the check could not establish either.
    """
    try:
        return stat.S_ISDIR(root.stat().st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        return exc


def completed_from(all_units: Sequence[str], remaining: Sequence[str] | None) -> list[str]:
    """The units a `partial` checkpoint says already landed, derived rather than carried.

    The shipped `PayloadFactory` hands a worker only `remaining_units` (§11.5), so "what is
    already done" has to be `all_units - remaining` — which is only meaningful because
    `all_units` is re-derived deterministically from the same tree by the same sorted walk.

    Derivation has one blind spot, and it is the reason every payload here ALSO carries an
    optional `completed_units`: a unit whose file has been deleted since it landed is missing
    from `all_units` too, so subtraction can never name it. The carried list can, which is what
    lets `preconditions_hold` notice that the tree no longer matches the index built from it.
    """
    if remaining is None:
        return []
    owed = set(remaining)
    return [unit for unit in all_units if unit not in owed]


def checkpointed_units(
    all_units: Sequence[str], remaining: Sequence[str] | None, carried: Sequence[str]
) -> list[str]:
    """The best available answer to "which units already landed": carried if the wiring supplied
    it, else derived. Carried wins because it survives the file being deleted."""
    return list(carried) if carried else completed_from(all_units, remaining)


def owed_from(all_units: Sequence[str], remaining: Sequence[str] | None) -> list[str]:
    """The units this invocation still owes, in walk order. No checkpoint ⇒ everything."""
    if remaining is None:
        return list(all_units)
    owed = set(remaining)
    return [unit for unit in all_units if unit in owed]


def paths_intact(root: Path, units: Iterable[str]) -> bool:
    """Do every checkpointed unit's files still exist? §7.1: "if its referenced paths no longer
    exist … return False so the phase re-runs from `phases.base_ref`"."""
    return all((root / unit).exists() for unit in units)


class ManifestDependency(WorkerOutput):
    """One dependency, already normalized into the ADR-0017 address space.

    Carries `manifest_path` because §3.1 step 5 requires every edge to name an `evidence_path`,
    and the manifest that declared the dependency IS that evidence.
    """

    manifest_path: str
    coordinate: Coordinate
    scope: str | None = None
    optional: bool = False


class InterrogateInput(WorkerInput):
    repo_id: RepoId
    worktree_path: str | None = None
    ignore_globs: tuple[str, ...] = DEFAULT_IGNORE_GLOBS
    remaining_units: tuple[str, ...] | None = Field(
        default=None,
        description="The checkpoint's owed manifest paths; None = no checkpoint, walk it whole",
    )
    completed_units: tuple[str, ...] = Field(
        default=(),
        description="The checkpoint's landed paths, when the phase's factory supplies them; "
        "empty falls back to `all_units - remaining_units` (see `completed_from`)",
    )


class InterrogateOutput(WorkerOutput):
    """What the runner persists into `manifests` and `coordinates` (§3.1 steps 2–3)."""

    repo_id: RepoId
    manifests: tuple[ManifestRef, ...] = ()
    dependencies: tuple[ManifestDependency, ...] = ()
    ecosystems: tuple[Ecosystem, ...] = ()
    findings: tuple[str, ...] = ()


@register_worker
class InterrogateWorker(BaseWorker[InterrogateInput, InterrogateOutput]):
    """One walk, one dispatch table, one output contract (ADR-0005)."""

    __slots__ = ()

    name: ClassVar[str] = "interrogate"
    phase: ClassVar[Phase] = Phase.SCAN
    input_model: ClassVar[type[WorkerInput]] = InterrogateInput
    output_model: ClassVar[type[WorkerOutput]] = InterrogateOutput

    async def preconditions_hold(
        self, ctx: WorkerContext, payload: InterrogateInput
    ) -> bool:
        """Is there prior interrogation state, and is the tree it describes still the same one?

        `False` for a fresh repo (no checkpoint: nothing to resume, walk it whole) and for a
        worktree that has gone away or lost a manifest the checkpoint claims to have parsed —
        both mean the phase must re-run from `phases.base_ref` rather than re-enter. `True` means
        the checkpointed manifests are still on disk, so re-entry may resume at `remaining_units`
        alone; a completed phase reaches that with nothing owed, which is a no-op by
        construction.
        """
        root = worktree_of(ctx, payload.worktree_path)
        if payload.remaining_units is None:
            return False
        if not await asyncio.to_thread(root.is_dir):
            return False
        units = await asyncio.to_thread(self._manifest_paths, root, payload.ignore_globs)
        done = checkpointed_units(units, payload.remaining_units, payload.completed_units)
        return await asyncio.to_thread(paths_intact, root, done)

    async def run(
        self, ctx: WorkerContext, payload: InterrogateInput
    ) -> WorkerResult[InterrogateOutput]:
        root = worktree_of(ctx, payload.worktree_path)
        presence = await asyncio.to_thread(worktree_presence, root)
        if isinstance(presence, OSError):
            return WorkerResult[InterrogateOutput](
                status="failed",
                error=WorkerError(
                    failure_class=FailureClass.TRANSIENT_INFRA,
                    retryable=True,
                    stderr_tail=(
                        f"could not determine whether worktree {root} exists: {presence}"
                    ),
                ),
            )
        if not presence:
            return WorkerResult[InterrogateOutput](
                status="failed",
                error=WorkerError(
                    failure_class=FailureClass.PREFLIGHT,
                    retryable=False,
                    stderr_tail=f"worktree {root} does not exist; run the clone worker first",
                ),
            )

        units = await asyncio.to_thread(self._manifest_paths, root, payload.ignore_globs)
        owed = owed_from(units, payload.remaining_units)
        completed = checkpointed_units(units, payload.remaining_units, payload.completed_units)

        manifests: list[ManifestRef] = []
        dependencies: list[ManifestDependency] = []
        findings: list[str] = []

        for index, rel in enumerate(owed):
            now = loop_now()
            if ctx.cancelled() or ctx.expired(now):
                return self._interrupted(
                    completed,
                    owed[index:],
                    manifests,
                    dependencies,
                    timed_out=ctx.expired(now),
                )
            parsed = await asyncio.to_thread(self._parse_one, root, rel, payload.repo_id)
            if parsed is None:
                continue
            ref, deps = parsed
            manifests.append(ref)
            dependencies.extend(deps)
            completed.append(rel)

        if not units:
            manifests.append(self._unknown_manifest(payload.repo_id))
            findings.append("no-manifest")

        return WorkerResult[InterrogateOutput](
            status="ok",
            output=self._output(payload.repo_id, manifests, dependencies, findings),
            completed_units=completed,
            evidence=[ref.path for ref in manifests],
        )

    # -- internals -----------------------------------------------------------------------

    def _manifest_paths(self, root: Path, ignore_globs: Sequence[str]) -> list[str]:
        """The walk's manifest candidates: files a REAL adapter claims, in sorted order."""
        return [
            rel
            for rel in walk_files(root, ignore_globs)
            if _real_adapter_for(root / rel) is not None
        ]

    def _parse_one(
        self, root: Path, rel: str, repo_id: str
    ) -> tuple[ManifestRef, list[ManifestDependency]] | None:
        """Parse one manifest. Synchronous on purpose: every file read in this module happens off
        the event loop, in `asyncio.to_thread`, so a slow disk cannot stall the wave."""
        path = root / rel
        adapter = _real_adapter_for(path)
        if adapter is None:  # pragma: no cover - the walk already filtered these out
            return None
        digest = sha256_file(path)
        parse_error: str | None = None
        deps: list[ManifestDependency] = []
        published: Coordinate | None = None
        # BOTH adapter calls are guarded: `publishes()` re-reads the same file and raises the
        # same `ManifestParseError` on a malformed one, so guarding only `parse()` turns a
        # low-confidence manifest into an exception that aborts the walk mid-repo.
        try:
            raws = adapter.parse(path)
            published = adapter.publishes(path)
        except ManifestParseError as exc:
            parse_error = str(exc)
            raws = []
        else:
            deps = [
                ManifestDependency(
                    manifest_path=rel,
                    coordinate=adapter.coordinate(raw),
                    scope=raw.scope,
                    optional=raw.optional,
                )
                for raw in raws
            ]
        ref = ManifestRef(
            repo_id=repo_id,
            path=rel,
            ecosystem=adapter.ecosystem,
            adapter=adapter.name,
            adapter_version=adapter.version,
            sha256=digest,
            publishes=published,
            dependency_count=len(deps),
            low_confidence=parse_error is not None,
            parse_error=parse_error,
        )
        return ref, deps

    def _unknown_manifest(self, repo_id: str) -> ManifestRef:
        """§3.1 step 2's unknown-ecosystem path: one synthesized, low-confidence `ManifestRef`."""
        return ManifestRef(
            repo_id=repo_id,
            path=UNKNOWN_MANIFEST_PATH,
            ecosystem=Ecosystem.UNKNOWN,
            adapter=_UNKNOWN_ADAPTER,
            adapter_version=1,
            sha256=sha256_of_nothing(),
            publishes=Coordinate(ecosystem=Ecosystem.UNKNOWN, group="", name=repo_id),
            dependency_count=0,
            low_confidence=True,
        )

    def _output(
        self,
        repo_id: str,
        manifests: Sequence[ManifestRef],
        dependencies: Sequence[ManifestDependency],
        findings: Sequence[str],
    ) -> InterrogateOutput:
        ecosystems = sorted({ref.ecosystem for ref in manifests}, key=lambda eco: eco.value)
        return InterrogateOutput(
            repo_id=repo_id,
            manifests=tuple(manifests),
            dependencies=tuple(dependencies),
            ecosystems=tuple(ecosystems),
            findings=tuple(findings),
        )

    def _interrupted(
        self,
        completed: Sequence[str],
        remaining: Sequence[str],
        manifests: Sequence[ManifestRef],
        dependencies: Sequence[ManifestDependency],
        *,
        timed_out: bool = False,
    ) -> WorkerResult[InterrogateOutput]:
        """Out of deadline or cancelled between manifests. Whatever parsed is real output, so it
        travels with the `partial` and re-entry starts at the manifest that did not.

        Nothing completed means nothing to resume from: a genuine `ctx.cancelled()` is an
        operator decision and stays `cancelled` (not an attempt), but `ctx.expired()` is a real
        timeout and must be a chargeable `timeout` result (§11) — conflating the two into
        `cancelled` would let a repo that times out on every attempt never escalate.
        """
        if not completed:
            return WorkerResult[InterrogateOutput](
                status="timeout" if timed_out else "cancelled",
                error=WorkerError(
                    failure_class=(
                        FailureClass.TIMEOUT if timed_out else FailureClass.TRANSIENT_INFRA
                    ),
                    retryable=True,
                    stderr_tail="cancelled before any manifest was parsed",
                ),
            )
        repo_id = manifests[0].repo_id if manifests else ""
        return WorkerResult[InterrogateOutput](
            status="partial",
            output=self._output(repo_id, manifests, dependencies, ()) if manifests else None,
            completed_units=list(completed),
            remaining_units=list(remaining),
        )


def sha256_of_nothing() -> str:
    """The digest of the empty byte string — what a synthesized manifest with no file hashes to.

    A real value rather than 64 zeroes: `ManifestRef.sha256` is `pattern=r"^[0-9a-f]{64}$"`, and a
    made-up constant that happens to satisfy the pattern would be a digest of nothing pretending
    to be a digest of something.
    """
    return sha256_bytes(b"")


def _real_adapter_for(path: Path) -> ManifestAdapter | None:
    """The adapter that CLAIMS this path, ignoring the priority-10 000 catch-all.

    `adapter_for` is total by design (§7.3) — `UnknownAdapter` matches anything — so "no adapter
    wanted this file" has to be expressed as "the only one that answered was the catch-all".
    """
    adapter = adapter_for(path)
    if adapter is None or adapter.name == _UNKNOWN_ADAPTER:
        return None
    return adapter
