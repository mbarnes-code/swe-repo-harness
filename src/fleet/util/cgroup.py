"""Process-tree cgroup v2 memory reader (SPEC §12.22 runtime RSS-sampling sub-clause, task B1).

**Scope: a pure reader, not a sampler.** This module resolves the CURRENT process's own cgroup
v2 path and reads its `memory.current`. It does nothing periodic, counts no breaches, and is not
wired into `PhaseRunner`/`resource_guard` -- that wiring (a genuine new periodic background task
plus a breach-counting halt policy) is task B2's scope (research-17-report.md §3c/§4), dispatched
separately after this lands and is reviewed.

**Assumption, stated explicitly rather than silently baked in: this reader targets cgroup v2
(the unified hierarchy) only.** research-18-report.md confirmed the actual sandbox this project
develops in runs cgroup v2 (`/proc/self/cgroup` returns a single `0::<path>` line). The
orchestrator's real production deployment target may differ -- a pure cgroup v1 host exposes
MULTIPLE numbered-hierarchy lines (`12:memory:/user.slice`, `11:cpu,cpuacct:/...`, one per
controller) and no `0::` line at all, with memory accounted under a differently-named file
(`memory.usage_in_bytes`, not `memory.current`) at a per-controller path this module does not
know how to build. Rather than guess a v1 path or silently read the wrong file, a host presenting
that shape is explicitly UNSUPPORTED here and fails loudly with `CgroupUnavailableError` naming
what was found -- per CLAUDE.md Rule 11, a magic assumption that can silently produce a wrong (or
zero) number is worse than a refusal. Adding real v1 support, if the orchestrator is ever deployed
on a v1-only host, is a disclosed follow-on, not something this module fakes.

**Finding: no per-PID enumeration is needed -- `memory.current` already aggregates the whole
process tree.** SPEC §12.22's text wants the orchestrator's own process PLUS its descendants
(worker subprocesses, `cpu_pool`'s `ProcessPoolExecutor` children). Two independent facts,
verified rather than assumed:

1. The cgroup v2 documentation for `memory.current` (`Documentation/admin-guide/cgroup-v2.rst`)
   defines it as "the total amount of memory currently being used by the cgroup **and its
   descendants**" -- it is a hierarchical, already-summed figure by design, not a per-process
   snapshot a caller must total up itself.
2. On Linux, a forked/exec'd child inherits its parent's cgroup membership unless something
   explicitly moves it (writes the child's PID to a different `cgroup.procs`) -- plain
   `subprocess`/`multiprocessing` spawning does not do this, and neither does the process-group
   isolation `fleet.util.proc.run` uses (`start_new_session=True` creates a new SESSION/process
   group for signal-delivery purposes -- SPEC §11.1 -- which is unrelated to cgroup membership).
   Verified empirically in this development sandbox (not merely reasoned): a plain `subprocess.
   run(..., start_new_session=True)` child and a `concurrent.futures.ProcessPoolExecutor` worker
   both read back the SAME `/proc/self/cgroup` line as their parent. `orchestrator/budgets.py`'s
   `new_cpu_pool` (`ProcessPoolExecutor`) and every `util/proc.py` subprocess spawn are exactly
   this shape, so the orchestrator's real descendants land in the same cgroup by construction --
   no explicit re-parenting exists anywhere in this codebase that would move them out.

So `read_process_tree_memory_bytes` below is the whole primitive: resolve the orchestrator's own
cgroup path once, read `memory.current` at that path. No `/proc/<pid>/cgroup` enumeration of
descendant PIDs is needed, and none is built here.
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_PROC_SELF_CGROUP = Path("/proc/self/cgroup")
"""Where a process resolves ITS OWN cgroup. Never another PID's -- this module answers "how much
memory has the calling process's cgroup (and everything sharing it) used", which per the module
docstring's finding is already the whole tree for this codebase's spawn patterns."""

DEFAULT_CGROUP_FS_ROOT = Path("/sys/fs/cgroup")
"""The cgroup v2 filesystem mount point. `/proc/self/cgroup`'s `0::<path>` is relative to this."""

_V2_LINE_PREFIX = "0::"

MEMORY_CURRENT_FILENAME = "memory.current"


class CgroupUnavailableError(RuntimeError):
    """This process's cgroup v2 `memory.current` could not be resolved or read.

    Raised rather than returning `0`/`None` (CLAUDE.md Rule 11) whenever:

    * `/proc/self/cgroup` is missing or unreadable;
    * its content is not the single-line cgroup v2 `0::<path>` form -- a v1-only host (multiple
      numbered-hierarchy lines, no `0::` line) is explicitly unsupported, see module docstring;
    * the resolved `memory.current` file is missing, unreadable, or does not contain a bare
      integer byte count.
    """


def resolve_own_cgroup_v2_relpath(*, proc_self_cgroup: Path = DEFAULT_PROC_SELF_CGROUP) -> str:
    """Parse `/proc/self/cgroup` for the single cgroup v2 unified-hierarchy line and return its
    path component (e.g. `/user.slice/user-1000.slice/session-5.scope`).

    A v2 host's `/proc/self/cgroup` is exactly one line, `0::<path>` (the `0` hierarchy ID is
    cgroup v2's fixed convention; the middle field -- the controller list -- is always empty
    under the unified hierarchy). Anything else -- zero `0::` lines (a pure v1 host, or an
    unreadable/empty file that still opened) or more than one (a malformed/unexpected file this
    module has no basis to interpret) -- is refused loudly rather than guessed at.
    """
    try:
        content = proc_self_cgroup.read_text()
    except OSError as exc:
        raise CgroupUnavailableError(
            f"could not read {proc_self_cgroup} to resolve this process's cgroup v2 path: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    lines = [line for line in content.splitlines() if line.strip()]
    v2_lines = [line for line in lines if line.startswith(_V2_LINE_PREFIX)]
    if len(v2_lines) != 1:
        raise CgroupUnavailableError(
            "expected exactly one cgroup v2 unified-hierarchy line ('0::<path>') in "
            f"{proc_self_cgroup}, found {len(v2_lines)} among {len(lines)} total line(s): "
            f"{lines!r}. This reader assumes cgroup v2 (unified hierarchy, per research-18's "
            "confirmation of this deployment's sandbox) -- a pure cgroup v1 host (multiple "
            "numbered-hierarchy lines, no '0::' line) is explicitly unsupported and must fail "
            "loudly here rather than guess a per-controller v1 path."
        )

    relpath = v2_lines[0][len(_V2_LINE_PREFIX) :]
    if not relpath.startswith("/"):
        raise CgroupUnavailableError(
            f"malformed cgroup v2 line in {proc_self_cgroup}: {v2_lines[0]!r} -- path component "
            "does not start with '/'"
        )
    return relpath


def read_process_tree_memory_bytes(
    *,
    proc_self_cgroup: Path = DEFAULT_PROC_SELF_CGROUP,
    cgroup_fs_root: Path = DEFAULT_CGROUP_FS_ROOT,
) -> int:
    """Bytes of memory currently charged to this process's cgroup -- which, per the module
    docstring's finding, already includes every descendant process spawned the way this codebase
    spawns them (no per-PID enumeration performed or needed).

    `proc_self_cgroup`/`cgroup_fs_root` are injectable so a test can point this at a real tmpfile
    and a real tmp directory tree rather than mocking `open()` -- see `tests/test_cgroup.py`.
    Production callers pass neither and get the real host paths.
    """
    relpath = resolve_own_cgroup_v2_relpath(proc_self_cgroup=proc_self_cgroup)
    memory_current_path = cgroup_fs_root / relpath.lstrip("/") / MEMORY_CURRENT_FILENAME
    try:
        raw = memory_current_path.read_text().strip()
    except OSError as exc:
        raise CgroupUnavailableError(
            f"could not read {memory_current_path} (resolved from {proc_self_cgroup}'s "
            f"'{_V2_LINE_PREFIX}{relpath}' line): {type(exc).__name__}: {exc}"
        ) from exc
    try:
        return int(raw)
    except ValueError as exc:
        raise CgroupUnavailableError(
            f"{memory_current_path} did not contain a bare integer byte count, got {raw!r}"
        ) from exc
