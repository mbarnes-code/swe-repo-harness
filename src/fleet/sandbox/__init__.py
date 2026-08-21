"""Isolation primitives: per-repo git worktrees and network-less Docker containers (ADR-0010).

A container and the cut-and-recut Phase 3/4 worktree it bind-mounts are named
`fleet-<run_id>-<repo>-<attempt>` by the SAME function (SPEC §3.3), so the two cannot drift apart
and either kind of orphan is attributable to a task by name alone — which is what makes
`fleet resume`'s reap step work without consulting the database first. The cross-phase repo
checkout is the second form, `fleet-<run_id>-<repo>` (`checkout_name`, ADR-0085): attempt-free,
because Phase 2 reads the tree Phase 1 cloned. Both begin with `run_prefix(run_id)`, which is the
whole of what the reap filters on.
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
    checkout_name,
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
    "checkout_name",
    "docker_run_argv",
    "run_prefix",
    "sandbox_name",
    "slug",
    "spec_for_attempt",
]
