"""The typed git surface every other module in the harness talks to (SPEC §3.2 step 6, ADR-0003).

Three properties are load-bearing, and each is a defect this module exists to make unreachable.

* **No shell, ever.** Every invocation is an argv list handed to `util.proc.run`, which is
  `create_subprocess_exec` with `start_new_session=True`. A branch name, a repo id, or a
  model-proposed path can therefore never be parsed as shell syntax, and a `git` that outlives its
  deadline is killed as a process *group* rather than leaked. No shell flag, no shell-spawning
  subprocess variant, and no string interpolation of a command anywhere in `fleet.vcs`;
  `tests/test_vcs.py` asserts exactly that on the source text, because "we remembered not to" is
  not an invariant.
* **Results are structured, not stdout.** Callers get `CommitInfo`, `DiffStat`, `bool`, or a SHA —
  never a blob of text they must re-parse. Ref reads that can legitimately miss (`resolve`) return
  `None`; everything else raises `GitCommandError`, so a failure is loud (Rule 11) and carries the
  argv, exit code, and stderr tail needed to diagnose it. That includes the four `check=False`
  probes (`resolve`, `ref_exists`, `apply_check`, `is_ancestor`): each answers a yes/no question
  about history, but `result.ok` is false for three different reasons — the process never
  started, it was killed at its deadline, or it ran and genuinely answered "no" — and only the
  last is a fact about the question asked (D42). Each probe checks `util.proc.no_verdict` first
  and raises `GitCommandError` on the first two; only a settled `ProcResult` is allowed to become
  the method's bool/`None` answer.
* **Nothing returned or raised can carry a credential.** `util.proc` redacts both output tails at
  capture, so every string this module returns is already scrubbed (§11.4). The gap that leaves is
  the *argv*: mirror remotes in this environment embed a plaintext `github_pat_…` in their origin
  URL, so a naive `f"git {' '.join(argv)} failed"` in an exception message would print the token
  that the log pipeline was careful never to write. `GitCommandError` therefore redacts its argv
  before storing it, and `Git.remote_url()` returns a redacted URL by construction.

Identity is passed per-invocation (`-c user.name=… -c user.email=…`) rather than read from the
host's git config: harness commits must be attributable and reproducible on a machine — a CI
container, a fresh worktree — where no global identity is configured, and a commit that fails
because `user.email` is unset is a Phase 2 outage with a confusing message.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from fleet.obs.redact import redact_text
from fleet.util.proc import CommandRunner, ProcResult, no_verdict, run

__all__ = [
    "DEFAULT_IDENTITY",
    "DEFAULT_TIMEOUT_S",
    "CommitInfo",
    "DiffStat",
    "FileStat",
    "Git",
    "GitCommandError",
    "GitError",
    "GitIdentity",
    "GitRefError",
    "redact_argv",
]

DEFAULT_TIMEOUT_S: Final = 600.0
"""Ceiling for a local git call. `deadline` (the worker's absolute clock) still wins when it is
earlier — `util.proc.run` takes the minimum, so a per-call timeout can only tighten."""

_UNIT: Final = "\x1f"
"""Field separator inside one `git log` record — a byte no path, subject, or trailer contains."""

_SUBSEP: Final = "\x1e"
"""Separator between repeated trailer values of the same key."""


@dataclass(frozen=True, slots=True)
class GitIdentity:
    """Who the harness's commits are authored by. Not the operator's identity: a commit made by
    the fleet must be greppable as such in the monorepo's history."""

    name: str = "fleet-harness"
    email: str = "fleet@localhost"

    def config_args(self) -> tuple[str, ...]:
        return (
            "-c",
            f"user.name={self.name}",
            "-c",
            f"user.email={self.email}",
            "-c",
            "commit.gpgsign=false",
        )


DEFAULT_IDENTITY: Final = GitIdentity()


def redact_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """Scrub every argument through the §11.4 redactor.

    Applied to the argv stored on an exception rather than to the argv that is executed: git must
    receive the real URL, and only the *rendering* of it must be safe.
    """
    return tuple(redact_text(part) for part in argv)


class GitError(RuntimeError):
    """Base for every failure in `fleet.vcs`. Callers catch this to classify a `FailureClass`."""


class GitCommandError(GitError):
    """A git invocation exited non-zero (Rule 11: never swallowed, never logged-and-continued).

    Carries the evidence an `attempts` row needs — redacted argv, exit code, stderr tail — so the
    repair loop can be handed verbatim error text without a transcript (CLAUDE.md guardrail 5).

    `started` is carried BESIDE `timed_out` rather than folded into it, because `util.proc.run`
    sets both flags for a call made after its deadline had already passed and a classifier reading
    only `timed_out` cannot then tell "we never asked" from "we asked and it ran too long". Those
    two answers cost a repo different things on the ADR-0014 ladder (see `base.clock_failure`), so
    the distinction has to survive the trip from `ProcResult` into the exception.

    `started` is a required keyword rather than a defaulted one: a default of `True` let a
    construction site that forgot to forward it silently claim a process that never launched
    actually ran and exited — exactly the misclassification this field exists to prevent (found
    in review as I5, `docs/superpowers/plans/review-36.md`, which cost `cli.py:3634` a real
    regression). Making the keyword required turns that omission into a `TypeError` at the call
    site instead of a wrong answer fed to the clock-failure classifier.
    """

    def __init__(
        self,
        argv: Sequence[str],
        exit_code: int,
        stderr_tail: str,
        *,
        cwd: Path | None = None,
        timed_out: bool = False,
        started: bool,
    ) -> None:
        self.argv = redact_argv(argv)
        self.exit_code = exit_code
        self.stderr = redact_text(stderr_tail)
        self.cwd = cwd
        self.timed_out = timed_out
        self.started = started
        detail = (
            "never started" if not started else "timed out" if timed_out else f"exit {exit_code}"
        )
        super().__init__(f"git {' '.join(self.argv[1:])} failed ({detail}): {self.stderr}".strip())


class GitRefError(GitError):
    """A ref or revision that must exist does not. Distinct from `GitCommandError` because the
    caller's response differs: a missing anchor is a resume-time reconciliation, not a retry."""


@dataclass(frozen=True, slots=True)
class CommitInfo:
    """One commit as the harness reads it: identity, subject, and parsed trailers.

    Trailers are *parsed* by git (`%(trailers:key=…)`), never matched against message text —
    SPEC §3.2 step 6.1 calls that form the authoritative one, and `--grep` merely the fast
    pre-filter, because a subject line quoting a trailer would fool a text match.
    """

    sha: str
    subject: str = ""
    trailers: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def trailer(self, key: str) -> str | None:
        """First value of `key`, or None. Repeated trailers are pathological but not fatal."""
        values = self.trailers.get(key)
        return values[0] if values else None


@dataclass(frozen=True, slots=True)
class FileStat:
    """One file's contribution to a diff. `binary` files report no line counts, and conflating
    that with "0 changed lines" would make a binary-only diff look empty to the §3.2 success
    criterion ("`git diff --stat` … is non-empty")."""

    path: str
    insertions: int
    deletions: int
    binary: bool = False


@dataclass(frozen=True, slots=True)
class DiffStat:
    """`git diff --numstat`, structured. `is_empty` is Phase 2's success criterion, not a
    convenience: a transform that changed nothing must fail rather than report success."""

    files: tuple[FileStat, ...] = ()

    @property
    def files_changed(self) -> int:
        return len(self.files)

    @property
    def insertions(self) -> int:
        return sum(f.insertions for f in self.files)

    @property
    def deletions(self) -> int:
        return sum(f.deletions for f in self.files)

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(f.path for f in self.files)

    @property
    def is_empty(self) -> bool:
        return not self.files


class Git:
    """A git CLI bound to one working directory (a worktree, a bare repo, or a clone).

    `runner` is injected (CLAUDE.md guardrail 3) so command construction is assertable without
    executing anything, and so every call inherits the caller's `ctx.deadline` and the
    `limits.subprocess` semaphore rather than re-deriving a timeout per call.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        runner: CommandRunner = run,
        git_bin: str = "git",
        identity: GitIdentity = DEFAULT_IDENTITY,
        deadline: float | None = None,
        timeout_s: float | None = DEFAULT_TIMEOUT_S,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.path = Path(path)
        self._runner = runner
        self._git = git_bin
        self.identity = identity
        self.deadline = deadline
        self.timeout_s = timeout_s
        self._env = None if env is None else dict(env)

    # -- invocation ---------------------------------------------------------------------
    def argv(self, args: Sequence[str], *, with_identity: bool = False) -> tuple[str, ...]:
        """The exact argv that would be executed. Public so a test can assert on it without a
        repo, and so nothing is tempted to rebuild a command as a string."""
        prefix = self.identity.config_args() if with_identity else ()
        return (self._git, "-C", str(self.path), *prefix, *args)

    async def exec(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        with_identity: bool = False,
        deadline: float | None = None,
        timeout_s: float | None = None,
    ) -> ProcResult:
        """Run one git command. Raises `GitCommandError` when `check` and it failed."""
        parts = self.argv(args, with_identity=with_identity)
        result = await self._runner(
            parts,
            cwd=self.path,
            env=self._env,
            deadline=self.deadline if deadline is None else deadline,
            timeout_s=self.timeout_s if timeout_s is None else timeout_s,
        )
        if check and not result.ok:
            raise GitCommandError(
                parts,
                result.exit_code,
                result.stderr_tail,
                cwd=self.path,
                timed_out=result.timed_out,
                started=result.started,
            )
        return result

    def _require_settled(self, result: ProcResult) -> None:
        """Raise when `result` establishes nothing about the question a `check=False` probe asked
        (D42). `no_verdict` orders `started` before `timed_out` — a call made past an already-passed
        deadline carries both flags, and reading `timed_out` first would misreport a command that
        never ran as one that ran too long — and `GitCommandError`'s own `detail` computation
        preserves that same ordering, so the raised message names the right cause.

        Every `check=False` probe in this class must call this BEFORE reading `result.ok`: a bool
        or `None` derived from an unsettled `ProcResult` is not the probe's answer, it is the
        fleet's clock reported as if it were one.
        """
        reason = no_verdict(result)
        if reason is not None:
            raise GitCommandError(
                result.argv,
                result.exit_code,
                result.stderr_tail,
                cwd=self.path,
                timed_out=result.timed_out,
                started=result.started,
            )

    async def text(
        self,
        args: Sequence[str],
        *,
        with_identity: bool = False,
        deadline: float | None = None,
        timeout_s: float | None = None,
    ) -> str:
        """Checked call returning stripped stdout — already redacted by `util.proc` at capture."""
        result = await self.exec(
            args, with_identity=with_identity, deadline=deadline, timeout_s=timeout_s
        )
        return result.stdout_tail.strip()

    # -- revisions and refs -------------------------------------------------------------
    async def resolve(self, rev: str) -> str | None:
        """Full SHA of `rev`, or None if it does not exist. The one ref read allowed to miss:
        "has this run's anchor been created yet?" is a question, not a failure.

        That miss must be a SETTLED "no such rev", not a rev-parse that never started or was
        killed at its deadline — collapsing the two is D42/D43: `cli.py` treats a `None` here as
        licence to force-reset a migration branch, and a timeout is not licence for that. So a
        probe with no verdict (`util.proc.no_verdict`) raises `GitCommandError` instead of
        returning `None`; only a rev-parse that actually ran and actually said "no such rev" may
        mean "does not exist".
        """
        result = await self.exec(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
                                 check=False)
        self._require_settled(result)
        sha = result.stdout_tail.strip()
        return sha if result.ok and sha else None

    async def rev_parse(self, rev: str) -> str:
        """Full SHA of `rev`; raises `GitRefError` when it is unknown (Rule 11)."""
        sha = await self.resolve(rev)
        if sha is None:
            raise GitRefError(f"revision {rev!r} does not resolve in {self.path}")
        return sha

    async def ref_exists(self, ref: str) -> bool:
        """Whether `ref` resolves. A `check=False` non-zero exit is only "no" once the call
        actually ran to completion (D42): a probe that never started or was killed at its
        deadline has not established that `ref` is absent, and `_require_settled` raises rather
        than let that indeterminate state be reported as `False`."""
        result = await self.exec(["show-ref", "--verify", "--quiet", ref], check=False)
        self._require_settled(result)
        return result.ok

    async def update_ref(self, ref: str, new_value: str, *, message: str | None = None) -> None:
        """Create or move a ref. This is how every fleet anchor and snapshot is written: a real
        ref, kept recoverable by the reflog, never a SHA copied into SQLite as the only record."""
        args = ["update-ref"]
        if message is not None:
            args += ["-m", message]
        args += [ref, new_value]
        await self.exec(args)

    async def delete_ref(self, ref: str) -> None:
        await self.exec(["update-ref", "-d", ref])

    async def list_refs(self, prefix: str) -> tuple[str, ...]:
        """Every ref under `prefix`, sorted by git. Used to derive the monotonic snapshot `seq`
        from git itself rather than from a counter that a crash would desynchronise."""
        out = await self.text(["for-each-ref", "--format=%(refname)", prefix])
        return tuple(line.strip() for line in out.splitlines() if line.strip())

    async def current_branch(self) -> str | None:
        """Branch name, or None in a detached worktree (which is the normal case here: worktrees
        are cut `--detach` by `sandbox/worktree.py`)."""
        result = await self.exec(["symbolic-ref", "--quiet", "--short", "HEAD"], check=False)
        name = result.stdout_tail.strip()
        return name if result.ok and name else None

    async def create_branch(self, name: str, start_point: str, *, force: bool = False) -> str:
        """Point `name` at `start_point`. Returns the resulting SHA."""
        args = ["branch", "--force", name, start_point] if force else \
            ["branch", name, start_point]
        await self.exec(args)
        return await self.rev_parse(name)

    async def checkout(self, rev: str, *, detach: bool = False) -> None:
        args = ["checkout", "--detach", rev] if detach else ["checkout", rev]
        await self.exec(args)

    async def remote_url(self, remote: str = "origin") -> str | None:
        """The remote's URL, **redacted**. Mirror remotes in this environment carry a plaintext
        `github_pat_…` in their origin URL (§11.4), so the unredacted form is deliberately not
        obtainable through this class — nothing in the harness needs it, and everything that
        prints it would leak it."""
        result = await self.exec(["remote", "get-url", remote], check=False)
        if not result.ok:
            return None
        return redact_text(result.stdout_tail.strip()) or None

    # -- worktree state -----------------------------------------------------------------
    async def is_dirty(self) -> bool:
        out = await self.text(["status", "--porcelain"])
        return bool(out.strip())

    async def merge_in_progress(self) -> bool:
        """`MERGE_HEAD` exists — the Git-local condition SPEC §3.2 step 6 uses to recover a
        crashed Phase 3 merge, checked "without consulting SQLite at all"."""
        return await self.resolve("MERGE_HEAD") is not None

    async def add_all(self) -> None:
        await self.exec(["add", "--all"])

    # -- patches ------------------------------------------------------------------------
    async def apply_check(self, patch: Path | str, *, reverse: bool = False) -> bool:
        """`git apply --check [--reverse] <patch>` as a boolean probe. Does not raise on a
        genuinely refused patch: "does this patch apply?" is the question, and a settled non-zero
        exit is the answer *No*.

        It DOES raise on an exit that settles nothing (D42, corrected here — this docstring used
        to claim "a non-zero exit is the answer No" without qualification, which is exactly the
        false claim D42 quotes: `exit 124` from a call that never started, or a kill at the
        deadline, is not the answer No, it is the fleet's clock). `_require_settled` checks
        `util.proc.no_verdict` first and raises `GitCommandError` for those two cases, so only a
        `git apply` that actually ran to completion can become this method's `bool`.

        With `reverse=True` this is guard (b) of SPEC §3.2 step 6.1 — it succeeds exactly when the
        patch's effect is present in the CURRENT tree, which is the only check that survives a
        rebase having dropped the hunk out from under a committed trailer.
        """
        args = ["apply", "--check"]
        if reverse:
            args.append("--reverse")
        args.append(str(patch))
        result = await self.exec(args, check=False)
        self._require_settled(result)
        return result.ok

    async def apply(self, patch: Path | str, *, index: bool = True) -> None:
        """`git apply [--index] <patch>`. Raises `GitCommandError` on refusal.

        `git apply` is the ONLY writer into a worktree (SPEC §3.2 step 6.6): no worker opens a
        source file for writing, so an out-of-tree write cannot reach a commit.

        With `--index`, git compares each target's *stat* against the index and refuses with
        "does not match index" when they differ — even when the content is identical. A probe that
        merely rewrote a file with its own bytes (or any operation landing in the same mtime
        granule) therefore produces a spurious refusal that looks exactly like a bad patch. So the
        index is refreshed first; `check=False` because `update-index --refresh` exits non-zero for
        a genuinely modified file, which is a normal state here and not an error. A real content
        mismatch survives the refresh and is still refused.
        """
        args = ["apply"]
        if index:
            await self.exec(["update-index", "--refresh", "-q"], check=False)
            args.append("--index")
        args.append(str(patch))
        await self.exec(args)

    # -- commits ------------------------------------------------------------------------
    async def commit(
        self,
        subject: str,
        *,
        trailers: Mapping[str, str] | None = None,
        body: str | None = None,
        allow_empty: bool = False,
        no_verify: bool = True,
    ) -> str:
        """Commit the index and return the new SHA — THE atomic point of SPEC §3.2 step 6.3.

        One tree covering every changed file, then a ref move by flock + rename(2): the branch tip
        names the new commit or the old one, never a subset of a multi-file patch. That is why no
        write-ahead journal is needed to detect partial application — it is not a reachable state.
        """
        args = ["commit"]
        if no_verify:
            args.append("--no-verify")
        if allow_empty:
            args.append("--allow-empty")
        args += ["-m", subject]
        if body:
            args += ["-m", body]
        for key, value in (trailers or {}).items():
            args += ["--trailer", f"{key}={value}"]
        await self.exec(args, with_identity=True)
        return await self.rev_parse("HEAD")

    async def log(
        self,
        rev_range: str,
        *,
        trailer_keys: Sequence[str] = (),
        limit: int | None = None,
        paths: Sequence[str] = (),
    ) -> tuple[CommitInfo, ...]:
        """Structured `git log` over an explicit range, with trailers parsed by git.

        `rev_range` is mandatory and unbounded ranges are the caller's choice, because the guard
        this feeds is only correct when it is SCOPED: SPEC §3.2 step 6.1 requires
        `<phases.pre_commit_sha>..migrate/<repo>`, "never … the branch's full history".
        """
        keys = tuple(trailer_keys)
        fmt = "%H" + _UNIT + "%s"
        for key in keys:
            fmt += _UNIT + f"%(trailers:key={key},valueonly,separator=%x1e)"
        args = ["log", "-z", f"--format={fmt}"]
        if limit is not None:
            args.append(f"--max-count={limit}")
        args.append(rev_range)
        if paths:
            args += ["--", *paths]
        out = await self.text(args)
        return _parse_log(out, keys)

    async def find_trailer_commit(self, rev_range: str, key: str, value: str) -> str | None:
        """SHA of the newest commit in `rev_range` carrying `key: value`, or None.

        Guard (a) of SPEC §3.2 step 6.1. It proves a patch was once committed **in this phase on
        this branch** — never that its effect survives, which is why `commits.guard()` refuses to
        skip on this alone.
        """
        for commit in await self.log(rev_range, trailer_keys=[key]):
            if value in commit.trailers.get(key, ()):
                return commit.sha
        return None

    # -- diffs --------------------------------------------------------------------------
    async def diff_stat(
        self, *args: str, staged: bool = False, paths: Sequence[str] = ()
    ) -> DiffStat:
        """`git diff --numstat` between whatever `args` name (nothing = worktree vs index)."""
        argv = ["diff", "--numstat"]
        if staged:
            argv.append("--cached")
        argv += list(args)
        if paths:
            argv += ["--", *paths]
        return _parse_numstat(await self.text(argv))

    # -- content addressing ---------------------------------------------------------------
    async def hash_object(self, path: str) -> str:
        """The blob SHA `path`'s bytes would have if committed as-is, without staging it.

        `git hash-object` reads straight off the filesystem — it does not care whether `path`
        is tracked, staged, or plain untracked, which is what makes it usable across a crash
        that can leave generated bytes on disk in any of those states (D27). Comparing this
        against `blob_at` costs a fixed-size 40-character SHA rather than the file's full
        content, so it stays correct for a file the 32 KiB tail-truncation `util.proc.run`
        applies to every capture (SPEC §11.3) would otherwise silently compare truncated.
        """
        return await self.text(["hash-object", "--", path])

    async def blob_at(self, rev: str, path: str) -> str | None:
        """The blob SHA committed at `path` in `rev`, or `None` when `path` does not exist there.

        Reads what a REF actually carries, never a worktree file — the distinction D27 exists to
        enforce. A crashed dispatch can leave correct bytes sitting on disk, untracked or staged
        but never committed; `<rev>:<path>` resolves only to a blob genuinely reachable from
        `rev`, so it cannot be fooled by what merely sits in the worktree.
        """
        result = await self.exec(["rev-parse", "--verify", "-q", f"{rev}:{path}"], check=False)
        if not result.ok:
            return None
        return result.stdout_tail.strip()

    # -- destructive, and deliberately explicit -----------------------------------------
    async def reset_hard(self, rev: str) -> None:
        """`git reset --hard <rev>`. Exact by construction: the anchor is a ref, so nothing is
        reconstructed from a stored diff (ADR-0024)."""
        await self.exec(["reset", "--hard", rev])

    async def clean(self, *, directories: bool = True, ignored: bool = True) -> None:
        """`git clean -fd[x]` — the untracked/ignored debris a killed `git apply` leaves."""
        flags = "-f"
        if directories:
            flags += "d"
        if ignored:
            flags += "x"
        await self.exec(["clean", flags])

    async def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """`git merge-base --is-ancestor`. Used to prove a rollback anchor is actually *behind*
        the tip it is about to rewind, so a "rollback" can never silently move history sideways.

        A rollback anchor is exactly the kind of claim D42 warns about: `RollbackAnchorError`
        downstream (`vcs/commits.py`) asserts "the anchor is not an ancestor of the current tip"
        as a fact about history, and a `False` derived from a probe that never started or was
        killed at its deadline would make that a false claim. `_require_settled` raises on both
        before either can be reported as *No*.
        """
        result = await self.exec(
            ["merge-base", "--is-ancestor", ancestor, descendant], check=False
        )
        self._require_settled(result)
        return result.ok

    async def rebase(self, onto: str) -> bool:
        """`git rebase <onto>`, run with the harness identity (rebase recommits every replayed
        patch, so a host with no git identity configured fails it exactly as it fails `commit`
        — verified empirically, not assumed).

        Returns `True` when the rebase completes cleanly: HEAD now sits on `onto` with this
        branch's commits replayed on top. This is the ONLY case where `Git` methods on this
        repo remain safe to call without checking rebase state first.

        Returns `False` on a CONFLICT that leaves the rebase mid-flight (`REBASE_HEAD` resolves;
        `.git/rebase-merge` or `.git/rebase-apply` exists) — a settled, meaningful "no" the
        caller must react to, the same probe shape as `apply_check`: a refusal git can coherently
        report is returned, not raised. The caller MUST call `abort_rebase()` before doing
        anything else with this repo; every other method on this class assumes no rebase is in
        progress. There is no `continue_rebase()` on this class (see module docstring context in
        D94's own ledger entry): a conflict is a stop condition for this primitive, not something
        it resolves.

        Raises `GitCommandError` for anything else that stops the rebase without leaving a
        conflict to resolve — an unknown `onto` (`fatal: invalid upstream`, exit 128), a dirty
        worktree (`error: cannot rebase: You have unstaged changes`, exit 1) — verified
        empirically that NEITHER of those leaves `REBASE_HEAD` resolvable, which is exactly the
        signal this method uses to tell "conflict, recoverable" from "never really started."

        Deliberately the two-argument `git rebase <onto>` form only, not the three-argument
        `git rebase --onto <newbase> <upstream> [<branch>]` transplant form: the caller this
        primitive is built for rebases an entire branch onto its updated target, it never needs
        to transplant a sub-range onto a different base, and adding the unused form now would be
        exactly the premature abstraction CLAUDE.md Rule 2 forbids.
        """
        result = await self.exec(["rebase", onto], check=False, with_identity=True)
        if result.ok:
            return True
        self._require_settled(result)  # raises on never-started / killed-at-deadline (D42)
        if await self.resolve("REBASE_HEAD") is not None:
            return False  # settled, genuine conflict — mid-flight, caller must abort or resolve
        raise GitCommandError(
            result.argv,
            result.exit_code,
            result.stderr_tail,
            cwd=self.path,
            timed_out=result.timed_out,
            started=result.started,
        )

    async def abort_rebase(self) -> None:
        """`git rebase --abort` — return to the exact pre-rebase state (git restores the
        original branch tip itself; nothing here needs to remember it).

        Raises `GitCommandError` (Rule 11) if there is no rebase in progress or the abort itself
        fails — a caller reaching for this after `rebase()` returned `False` and finding git says
        there is nothing to abort is a real surprise (a second caller raced it, or state drifted)
        and must not be swallowed.
        """
        await self.exec(["rebase", "--abort"])

    async def revert(self, sha: str, *, mainline: int = 1) -> bool:
        """`git revert --no-commit -m <mainline> <sha>` — stage a revert without committing it.

        SPEC §3.1's hoist rollback (`docs/SPEC.md:606-610`, D111 Leg B): "revert its merge commit
        on the integration branch with `git revert -m 1`, carrying the standard `Fleet-*` commit
        trailers like any other mutation (§3.2 step 6)". `--no-commit` is what makes that
        possible — a plain `git revert` writes its own commit message and gives the caller no
        hook to add trailers, so this method only ever stages the revert into the index
        (mirroring `apply()`'s split from `commit()`); `vcs.commits.revert_and_commit` does the
        follow-up `commit()` call with the caller's subject and `FleetTrailers`.

        `mainline` is git's 1-based parent index, passed through even for an ordinary
        single-parent commit — verified empirically that git 2.43 accepts `-m 1` there (parent 1
        is simply the only parent) and only refuses a mainline number the commit does not
        actually have ("does not have parent N", exit 128, surfaced below as an ordinary
        `GitCommandError`). This method does not itself distinguish merge from non-merge commits;
        git already does.

        Returns `True` when the revert stages cleanly (the index now holds the reverted tree,
        ready to commit). Returns `False` on a CONFLICT that leaves it mid-flight (`REVERT_HEAD`
        resolves) — a settled, meaningful "no", the same probe shape as `rebase()`: the caller
        MUST call `abort_revert()` before doing anything else with this repo. Verified
        empirically that `REVERT_HEAD` also resolves after a CLEAN `--no-commit` revert (git
        leaves the sequencer state around until something clears it — a subsequent `commit()`
        clears it the same way `git revert --continue` would), so the `False` branch below is
        reached only via git's own non-zero exit; `REVERT_HEAD` is then read only to confirm a
        genuine, resolvable conflict rather than some other failure that never got that far,
        exactly the way `rebase()` reads `REBASE_HEAD`.

        Raises `GitCommandError` for anything else: an unknown `sha`, a `mainline` the commit
        does not have, or an unsettled probe (D42, via `_require_settled`).
        """
        result = await self.exec(["revert", "--no-commit", "-m", str(mainline), sha], check=False)
        if result.ok:
            return True
        self._require_settled(result)  # raises on never-started / killed-at-deadline (D42)
        if await self.resolve("REVERT_HEAD") is not None:
            return False  # settled, genuine conflict — mid-flight, caller must abort or resolve
        raise GitCommandError(
            result.argv,
            result.exit_code,
            result.stderr_tail,
            cwd=self.path,
            timed_out=result.timed_out,
            started=result.started,
        )

    async def abort_revert(self) -> None:
        """`git revert --abort` — return to exactly the pre-revert state (git restores the index
        and worktree itself; nothing here needs to remember them).

        Raises `GitCommandError` (Rule 11) if there is no revert in progress or the abort itself
        fails, mirroring `abort_rebase()`.
        """
        await self.exec(["revert", "--abort"])

    async def push_force_with_lease(self, remote: str, branch: str, *, expected_sha: str) -> None:
        """`git push --force-with-lease=<branch>:<expected_sha> <remote> <branch>`.

        The EXPLICIT `<branch>:<expected-sha>` form, never the bare `--force-with-lease` flag.
        The bare flag's safety depends on this repo's remote-tracking ref
        (`refs/remotes/<remote>/<branch>`) already being current via a prior `fetch` — a caller
        that skipped the fetch gets "force-with-lease" in name only, no different from `--force`.
        The explicit form instead has the SERVER compare its own current tip against
        `expected_sha` directly (e.g. the tip this harness last read back from `Forge.view()` for
        this PR) — the safety property does not depend on this repo's local state at all, which
        is what makes it correct without a fetch and directly testable
        (`tests/test_vcs.py::test_push_force_with_lease_refuses_when_the_remote_moved_past_the_expected_sha`
        proves the server-side refusal, not a client-side guess).

        `expected_sha` must be a real commit SHA the caller believes the remote branch currently
        points at — **never** the empty string or the all-zero SHA (`"0" * 40`). That value is
        git's own sentinel for "the ref must NOT currently exist yet", and passing it is git's
        documented way to force-with-lease a BRAND NEW ref into existence: verified empirically
        that a branch which exists locally but has never been pushed accepts a push with the
        all-zero lease with exit 0, silently creating the remote branch
        (`tests/test_vcs.py::test_push_force_with_lease_refuses_the_all_zero_sentinel_on_an_unpushed_branch`).
        That is exactly the "silent create fallback" the paragraph below says does not exist, so
        this method refuses the sentinel client-side with `ValueError` before git ever runs,
        rather than let it slip through as an unremarkable success. This primitive's only
        intended purpose is updating a branch that already exists on the remote — D94's eventual
        caller always reads a real tip back from `Forge.view()` first — so there is no legitimate
        call site for the "create" sentinel here.

        Raises `GitCommandError` on any OTHER refusal: the remote branch moved past
        `expected_sha` (`! [rejected] ... (stale info)`, exit 1 — the exact property this method
        exists to guarantee), a genuinely nonexistent LOCAL branch (`src refspec ... does not
        match any` — a plain refspec-resolution failure, not lease-specific), or a genuine
        network/auth failure. All are equally "this push did not happen," and the caller cannot
        safely treat any of them as partial success — it must re-read the forge (`Forge.view`)
        before deciding what to do next, so raising with `.stderr` naming the reason already
        gives it everything it needs. Deliberately does NOT collapse to a bool the way `rebase`
        does: unlike a conflict, there is nothing here for THIS method's caller to resolve in
        place.

        No `with_identity` — push does not create commit objects.
        """
        if set(expected_sha) <= {"0"}:
            raise ValueError(
                f"push_force_with_lease: expected_sha={expected_sha!r} is empty or the all-zero "
                "sentinel, which tells git the remote ref must NOT exist yet and would silently "
                "create it on push instead of refusing — pass a real SHA read back from the "
                "remote (e.g. via Forge.view())"
            )
        await self.exec(["push", f"--force-with-lease={branch}:{expected_sha}", remote, branch])


def _parse_log(out: str, keys: Sequence[str]) -> tuple[CommitInfo, ...]:
    """NUL-delimited records, `\\x1f`-delimited fields. Trailer-less commits yield empty tuples
    rather than being dropped, so a caller counting commits sees the real history."""
    commits: list[CommitInfo] = []
    for record in out.split("\0"):
        chunk = record.strip("\n")
        if not chunk.strip():
            continue
        fields = chunk.split(_UNIT)
        sha = fields[0].strip()
        if not sha:
            continue
        subject = fields[1] if len(fields) > 1 else ""
        trailers: dict[str, tuple[str, ...]] = {}
        for index, key in enumerate(keys, start=2):
            raw = fields[index] if len(fields) > index else ""
            values = tuple(v.strip() for v in raw.split(_SUBSEP) if v.strip())
            if values:
                trailers[key] = values
        commits.append(CommitInfo(sha=sha, subject=subject, trailers=trailers))
    return tuple(commits)


def _parse_numstat(out: str) -> DiffStat:
    files: list[FileStat] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, removed, path = parts[0], parts[1], parts[-1]
        binary = added == "-" or removed == "-"
        files.append(
            FileStat(
                path=path,
                insertions=0 if binary else int(added or 0),
                deletions=0 if binary else int(removed or 0),
                binary=binary,
            )
        )
    return DiffStat(files=tuple(files))
