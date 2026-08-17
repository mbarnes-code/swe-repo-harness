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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from fleet.sandbox.worktree import run_prefix, sandbox_name
from fleet.util.fs import require_free_space
from fleet.util.proc import CommandRunner, ProcResult, run

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
        after crashes and at `fleet resume`, where "already gone" is the expected case."""
        result = await self._runner(
            [self._docker, "rm", "--force", name], timeout_s=TEARDOWN_TIMEOUT_S
        )
        return result.ok

    async def list_by_prefix(self, prefix: str, *, timeout_s: float = 30.0) -> list[str]:
        """Names of containers (running or not) whose name starts with `prefix`."""
        result = await self._runner(
            [
                self._docker,
                "ps",
                "--all",
                "--filter",
                f"name=^{prefix}",
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
    ) -> list[str]:
        """Remove every `fleet-<run_id>-*` container no live task claims (SPEC §11.5 step 2).

        `live_names` is required for the same reason it is on `WorktreeManager.reap`: a reaper
        that assumes nothing is live kills a running build.
        """
        live = set(live_names)
        reaped: list[str] = []
        for name in await self.list_by_prefix(run_prefix(run_id), timeout_s=timeout_s):
            if name in live:
                continue
            await self.remove(name)
            reaped.append(name)
        return reaped
