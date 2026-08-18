"""`docker run --network=none --user ...`: caps, timeout, guaranteed teardown (ADR-0010, §3.3).

Verification never touches the network, so a build that "passes" by fetching a dependency from
the internet is impossible by construction — `--network=none` is not a hardening nicety, it is
what makes a green build evidence that the vendored/declared deps are complete.

Three things are enforced here rather than left to a caller:

* **The caps come from config, and they are always present.** `verify.container_memory` (8g) and
  `verify.container_cpus` (4.0) are half of the §11.3 host-memory arithmetic
  (`concurrency.docker × verify.container_memory + budgets.max_rss_mb ≤ max_host_rss_mb`); an
  uncapped container silently voids a ceiling startup already refused to violate.
* **Teardown is guaranteed, and it is by NAME.** Killing the `docker run` client does not stop
  the container — on a deadline breach or a cancel, the runner "kills the container by name
  (`fleet-<run_id>-*`)" (SPEC §11.1). So every container is named `fleet-<run_id>-<repo>-<attempt>`
  (the same string as its worktree, from `sandbox/worktree.py`) and `run()` force-removes that
  name in a `finally`, with a FRESH short deadline — the expired one would make teardown a no-op.
* **Docker is reached through an injected `CommandRunner`.** No SDK, no daemon socket, no import
  of `docker`. Command construction is therefore unit-testable with a fake runner and no daemon,
  which is the difference between "we tested the flags" and "we tested nothing".
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from fleet.sandbox.worktree import run_prefix, sandbox_name
from fleet.util.fs import require_free_space
from fleet.util.proc import CommandRunner, ProcResult, no_verdict, run

DEFAULT_MEMORY = "8g"
DEFAULT_CPUS = "4.0"
DEFAULT_NETWORK = "none"
TEARDOWN_TIMEOUT_S = 30.0


def current_user_spec() -> str:
    """`--user $(id -u):$(id -g)` (SPEC §3.3 step 4): the container writes into a bind-mounted
    worktree owned by the host user, and a root-written build artifact is a file the host cannot
    clean up afterwards."""
    return f"{os.getuid()}:{os.getgid()}"


@dataclass(frozen=True, slots=True)
class Mount:
    """One bind mount. `read_only` is the ADR-0010 toolchain-cache case; the Bazel disk and
    repository caches are deliberately read-write and shared across containers (SPEC §3.4)."""

    source: Path
    target: str
    read_only: bool = False

    def to_flag(self) -> str:
        suffix = ":ro" if self.read_only else ""
        return f"--volume={self.source}:{self.target}{suffix}"


@dataclass(frozen=True, slots=True)
class ContainerSpec:
    """Everything one sandboxed command needs. Built from `config/fleet.yaml#verify`."""

    image: str
    name: str
    command: tuple[str, ...]
    mounts: tuple[Mount, ...] = ()
    workdir: str | None = None
    memory: str = DEFAULT_MEMORY
    cpus: str = DEFAULT_CPUS
    network: str = DEFAULT_NETWORK
    user: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    min_free_bytes: int = 0
    """`preflight.min_free_bytes`, checked against every bind mount's volume before the container
    starts (§11.3: "re-checked before EVERY clone and EVERY container start, not once at
    startup"). It belongs on the spec rather than in the caller because the thing that fills the
    volume is what the container writes — the worktree and the shared Bazel caches, which are
    precisely the mounts. `0` disables the gate; `config/fleet.yaml` supplies the real floor."""


def spec_for_attempt(
    *,
    run_id: UUID | str,
    repo: str,
    attempt: int,
    image: str,
    command: Sequence[str],
    worktree: Path,
    container_workdir: str = "/work",
    memory: str = DEFAULT_MEMORY,
    cpus: str = DEFAULT_CPUS,
    network: str = DEFAULT_NETWORK,
    extra_mounts: Iterable[Mount] = (),
    env: Mapping[str, str] | None = None,
    min_free_bytes: int = 0,
    name: str | None = None,
) -> ContainerSpec:
    """The §3.3 step 4 shape: the task's worktree bind-mounted read-write.

    `name` defaults to `sandbox_name(run_id, repo, attempt)` — identical to the worktree's — for
    every caller that does not override it (every `test_sandbox.py` assertion; `rdepverify`'s use
    of the same convention). A caller whose retry loop can re-issue this SAME `(run_id, repo,
    attempt)` more than once — `buildverify._argv`, across an ADR-0014 `TRANSIENT_INFRA` retry —
    should pass its own: the default is deterministic across those retries, and a container a dead
    daemon left registered under it turns the next `docker run --name=<same>` into a PERMANENT
    name-conflict 125 that no amount of retrying removes.
    """
    mounts = (Mount(source=worktree, target=container_workdir), *extra_mounts)
    return ContainerSpec(
        image=image,
        name=name if name is not None else sandbox_name(run_id, repo, attempt),
        command=tuple(command),
        mounts=mounts,
        workdir=container_workdir,
        memory=memory,
        cpus=cpus,
        network=network,
        user=current_user_spec(),
        env=dict(env or {}),
        min_free_bytes=min_free_bytes,
    )


def docker_run_argv(spec: ContainerSpec, *, docker_bin: str = "docker") -> list[str]:
    """The exact argv. A list, never a string — a repo name is never parsed as shell syntax."""
    argv = [
        docker_bin,
        "run",
        "--rm",
        f"--name={spec.name}",
        f"--network={spec.network}",
        f"--memory={spec.memory}",
        f"--cpus={spec.cpus}",
    ]
    if spec.user:
        argv.append(f"--user={spec.user}")
    if spec.workdir:
        argv.append(f"--workdir={spec.workdir}")
    argv.extend(mount.to_flag() for mount in spec.mounts)
    argv.extend(f"--env={key}={value}" for key, value in sorted(spec.env.items()))
    argv.append(spec.image)
    argv.extend(spec.command)
    return argv


@dataclass(frozen=True, slots=True)
class ContainerReapFailure:
    """One `reap()` entry that was attempted and could not be verified removed — distinct from a
    container `reap()` never attempted because a live task still claims it (see
    `ContainerReapResult`)."""

    name: str
    reason: str


@dataclass(frozen=True, slots=True)
class ContainerReapResult:
    """The outcome of one `ContainerSandbox.reap()` sweep, kept honest about partial success
    (Rule 9/Rule 11; docs/INTEGRATION_HONESTY.md D29/D34-D45, the "four-state collapse" family,
    and D32, the open container-leak defect this directly touches).

    Mirrors `sandbox.worktree.ReapResult`/`ReapFailure` in shape — same problem, one layer over —
    but is a SEPARATE type on purpose rather than an import: containers and worktrees share only
    a naming scheme (`sandbox_name`/`run_prefix`, imported explicitly above), not a
    failure-reporting contract. Coupling the two would mean a future field added to worktree
    reaping (a different lifecycle, different failure modes — `git worktree remove` vs
    `docker rm`) silently changes this module's public return type too. Each dataclass here is a
    handful of lines; that duplication is cheaper than the coupling.

    `reaped` names containers this call actually removed (or found already gone — `docker rm` on
    a missing container is treated as success, matching `remove()`). `failed` names containers it
    attempted and could not verify removed, each with a reason distinguishing an environment
    fault (docker was never even invoked) from a settled docker-level refusal. A container spared
    because a live task still claims it appears in NEITHER list — "deliberately not attempted"
    and "attempted and unresolved" are different facts and must not collapse into one.
    """

    reaped: list[str]
    failed: list[ContainerReapFailure]

    @property
    def complete(self) -> bool:
        """True iff every dead container this sweep found was actually removed."""
        return not self.failed


class ContainerSandbox:
    """Runs commands in throwaway containers and reaps whatever a crash left behind."""

    def __init__(self, *, runner: CommandRunner = run, docker_bin: str = "docker") -> None:
        self._runner = runner
        self._docker = docker_bin

    async def available(self, *, timeout_s: float = 10.0) -> bool:
        """Is a daemon actually reachable? Callers use this to fail a phase loudly (Rule 11)
        rather than to skip verification quietly."""
        result = await self._runner(
            [self._docker, "version", "--format", "{{.Server.Version}}"], timeout_s=timeout_s
        )
        return result.ok

    async def run(
        self,
        spec: ContainerSpec,
        *,
        deadline: float | None = None,
        timeout_s: float | None = None,
        log_dir: Path | None = None,
    ) -> ProcResult:
        """Run `spec` to completion, then guarantee the container is gone.

        The `finally` removal uses its own timeout because on the path that needs it most — the
        deadline breach — `deadline` is already in the past, and a teardown inheriting it would
        never start (`util/proc.run` refuses to spawn past the deadline).

        The free-space gate fires BEFORE `docker run`, and it raises rather than returning a
        failed `ProcResult`: a refusal to start is not a container that exited non-zero, and a
        caller that could not tell them apart would charge a retry rung for a full volume.
        """
        for mount in spec.mounts:
            require_free_space(
                mount.source, spec.min_free_bytes, operation=f"docker run {spec.name}"
            )
        argv = docker_run_argv(spec, docker_bin=self._docker)
        try:
            return await self._runner(
                argv, deadline=deadline, timeout_s=timeout_s
            )
        finally:
            await self.remove(spec.name)

    async def stop(self, name: str, *, stop_grace_s: int = 10) -> bool:
        """`docker stop` — SIGTERM then SIGKILL inside the container, mirroring `util/proc`."""
        result = await self._runner(
            [self._docker, "stop", f"--time={stop_grace_s}", name], timeout_s=TEARDOWN_TIMEOUT_S
        )
        return result.ok

    async def remove(self, name: str) -> bool:
        """Force-remove by name. A container that is already gone is not an error — teardown runs
        after crashes and at `fleet resume`, where "already gone" is the expected case.

        Bare bool on purpose: `run()`'s `finally`, `rdepverify.on_cancel`, and
        `buildverify._sweep_containers` are all fire-and-forget cleanup callers — none inspects
        the return value, and the latter two already wrap the call in
        `contextlib.suppress(OSError, ...)`. Changing this method to raise on failure (mirroring
        `WorktreeManager.remove`) would change behaviour for callers outside this module's lane
        for no benefit to them. `reap()`, the one caller that DOES need to know why a removal
        failed, gets that through `_remove_with_reason` below instead of through this method's
        contract.
        """
        return await self._remove_with_reason(name) is None

    async def _remove_with_reason(self, name: str) -> str | None:
        """`None` on a verified removal (or "already gone"); otherwise a reason string a caller
        can report, distinguishing an environment fault from a settled docker-level refusal.

        `asyncio.create_subprocess_exec` (`util/proc.run`'s `_run_locked`) is called unguarded —
        by deliberate design, D38: each call site adds its own guard rather than `proc.run`
        catching for everyone. A missing `docker` binary (`FileNotFoundError`), a `PermissionError`
        on `cwd`, or OS resource exhaustion would otherwise propagate as a raw `OSError` straight
        out of `reap()`'s sweep loop, aborting it mid-iteration and discarding both the removals
        already made and every remaining entry — the same abort-mid-sweep shape
        `WorktreeManager.remove`/`reap` were fixed against in `4a421a3`/`ec31b0f`. Caught here
        instead, so `reap()` can continue past it.

        `no_verdict` is checked before `result.ok` for the same reason it is everywhere else in
        this family (ADR-0067 part 4): a call that never started or was killed at its deadline
        answers nothing about whether docker refused, and reporting that silence as a refusal
        would be a verdict this code never established.
        """
        try:
            result = await self._runner(
                [self._docker, "rm", "--force", name], timeout_s=TEARDOWN_TIMEOUT_S
            )
        except OSError as exc:
            return (
                f"docker rm --force {name} never ran: environment fault spawning "
                f"{self._docker!r} ({type(exc).__name__}: {exc}), not a docker-level refusal"
            )
        unsettled = no_verdict(result)
        if unsettled is not None:
            return f"docker rm --force {name} did not settle: {unsettled}"
        if not result.ok:
            return (
                f"docker rm --force {name} failed (exit {result.exit_code}): {result.stderr_tail}"
            )
        return None

    async def list_by_prefix(self, prefix: str, *, timeout_s: float = 30.0) -> list[str]:
        """Names of containers (running or not) whose name starts with `prefix`.

        Docker's `name` filter is a REGEX, not a literal prefix, and `prefix` here is built by
        `sandbox_name`/`slug` (`sandbox/worktree.py`), which deliberately PRESERVES `.` — a regex
        metacharacter — in a repo id (`my.repo.js` stays `my.repo.js`). Unescaped, `^{prefix}`
        matches any character at each `.` position, so a sweep for repo `a.b` also matches a
        sibling container named `...aXb...`: `re.escape` is what makes the filter match this
        prefix's literal characters and nothing else. Getting this wrong is not cosmetic — the
        caller (`ContainerSandbox.reap`, `BuildverifyWorker.on_cancel`) force-removes every name
        this returns, so an over-matching filter `docker rm --force`s a DIFFERENT repo's live
        container.
        """
        result = await self._runner(
            [
                self._docker,
                "ps",
                "--all",
                "--filter",
                f"name=^{re.escape(prefix)}",
                "--format",
                "{{.Names}}",
            ],
            timeout_s=timeout_s,
        )
        if not result.ok:
            return []
        return [line.strip() for line in result.stdout_tail.splitlines() if line.strip()]

    async def reap(
        self, *, run_id: UUID | str, live_names: Iterable[str], timeout_s: float = 30.0
    ) -> ContainerReapResult:
        """Remove every `fleet-<run_id>-*` container no live task claims (SPEC §11.5 step 2).

        `live_names` is required for the same reason it is on `WorktreeManager.reap`: a reaper
        that assumes nothing is live kills a running build.

        Previously this appended every candidate to `reaped` unconditionally, ignoring
        `remove()`'s own bool verdict — a `docker rm` that failed was reported reaped while the
        container still existed, a verdict the code never established (the "four-state collapse"
        family, D29/D34-D45) and directly adjacent to D32, the open container-leak defect: a
        caller told "these were reaped" had no way to know a leak remained.

        Now each candidate is attempted through `_remove_with_reason`, which distinguishes a
        verified removal from an attempted-and-failed one (with a reason) and from a raw `OSError`
        an unguarded subprocess spawn could otherwise raise mid-sweep (D38; see
        `_remove_with_reason`'s docstring) — one entry's failure does not stop the sweep or
        discard removals already made, matching `WorktreeManager.reap`'s fix in `4a421a3`. A
        container spared because it is in `live_names` is filtered out before any attempt, so it
        lands in neither `reaped` nor `failed` — "deliberately spared" is a third fact, not a
        failure.
        """
        live = set(live_names)
        reaped: list[str] = []
        failed: list[ContainerReapFailure] = []
        for name in await self.list_by_prefix(run_prefix(run_id), timeout_s=timeout_s):
            if name in live:
                continue
            reason = await self._remove_with_reason(name)
            if reason is not None:
                failed.append(ContainerReapFailure(name=name, reason=reason))
                continue
            reaped.append(name)
        return ContainerReapResult(reaped=reaped, failed=failed)
