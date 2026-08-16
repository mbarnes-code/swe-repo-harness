"""Isolation primitives: per-repo git worktrees and network-less Docker containers (ADR-0010).

Both are named `fleet-<run_id>-<repo>-<attempt>` by the SAME function (SPEC §3.3), so a worktree
and the container that bind-mounts it cannot drift apart and either kind of orphan is
attributable to a task by name alone — which is what makes `fleet resume`'s reap step work
without consulting the database first.
"""

from __future__ import annotations

from fleet.sandbox.container import (
    ContainerSandbox,
    ContainerSpec,
    Mount,
    docker_run_argv,
    spec_for_attempt,
)
from fleet.sandbox.worktree import (
    NAME_PREFIX,
    Worktree,
    WorktreeError,
    WorktreeManager,
    run_prefix,
    sandbox_name,
    slug,
)

__all__ = [
    "NAME_PREFIX",
    "ContainerSandbox",
    "ContainerSpec",
    "Mount",
    "Worktree",
    "WorktreeError",
    "WorktreeManager",
    "docker_run_argv",
    "run_prefix",
    "sandbox_name",
    "slug",
    "spec_for_attempt",
]
