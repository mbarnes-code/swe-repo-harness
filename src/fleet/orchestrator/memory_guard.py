"""`HostMemorySampler`: a genuine periodic background task that samples host memory on its own
timer and feeds `PhaseRunner`'s `resource_guard` (SPEC §12.22 runtime RSS-sampling sub-clause,
task 27 / round VI B2).

**Scope, precisely.** Task 26 (B1, merged) built two PURE, injectable-seam-based readers --
`fleet.util.cgroup.read_process_tree_memory_bytes` (this process's cgroup v2 `memory.current`,
which already aggregates the whole process tree per that module's own finding) and
`fleet.sandbox.containerstats.ContainerStatsReader` (sums every running `fleet-<run_id>-*`
container's `docker stats` MEM USAGE) -- and deliberately did nothing periodic and touched
nothing in `PhaseRunner`. This module is that wiring: a real `asyncio.create_task`-based
background loop that ticks on its own schedule, independent of repo-dispatch activity, plus the
`resource_guard()` implementation that reads what it last found. **It does NOT build the
end-to-end adversarial 50k-file (or ADR'd smaller) proof test -- that is task 28 (B3), dispatched
separately after this lands and is reviewed** (Rule 13: this task alone does not close §12.22 or
its RSS-sampling sub-clause).

**Which ceilings this enforces (task 29 / round VI closes the gap task 27 disclosed here).**
`BudgetsSection` (`settings.py`) carries TWO memory ceilings: `max_rss_mb` ("orchestrator process
only") and `max_host_rss_mb` ("whole tree + containers"). Task 27 enforced only the second; this
module now enforces BOTH, independently, on every tick:

* `max_host_rss_mb`, compared against `cgroup_reader() + container_reader.read_total(run_id)` --
  exactly "the whole process tree plus every container" the ceiling's own comment names, and
  exactly what task 26's two readers together measure. Unchanged from task 27.
* `max_rss_mb`, compared against `rss_reader()` -- the orchestrator process's OWN peak RSS, via
  `resource.getrusage(resource.RUSAGE_SELF).ru_maxrss` by default (`read_own_rss_bytes` below).

**Why `resource.getrusage` is the right primitive for `max_rss_mb`, not the anti-pattern §12.22
names.** §12.22's literal text bans `getrusage` for sampling "the whole process tree plus every
container" -- its own worked example is "a variant test that inflates only the pool children and
the containers must fail this criterion, which is exactly what a `getrusage`-only implementation
would pass": `getrusage(RUSAGE_SELF)` cannot see a child's or a container's memory, so it wrongly
passes a breach confined to either. That is a statement about the `max_host_rss_mb` ceiling
specifically. §11.3 confirms this reading directly rather than leaving it inferred: *"The host
ceiling is the one that matters, and it is not `resource.getrusage`... `getrusage` is retained
only as the per-process number in the same event"* -- i.e. `getrusage` is not banned outright,
it is retained, specifically as "the per-process number", which is exactly `max_rss_mb`'s own
`settings.py` comment ("orchestrator process only"). Task 26's cgroup reader is unsuited to this
ceiling for the reverse reason: cgroup v2 `memory.current` aggregates the WHOLE tree by
construction (task 26's own confirmed finding, `util/cgroup.py`'s module docstring) -- there is
no cgroup-based way to isolate "just the orchestrator" from what it shares a cgroup with, so it
cannot answer "the orchestrator's own RSS alone" the way `getrusage` naturally does. Using it
for `max_rss_mb` would not be a narrower/safer check, it would be a routinely false one:
`max_rss_mb`'s default (4096) is smaller than `max_host_rss_mb`'s (12288) precisely because pool
children and containers are expected to add real memory on top of the orchestrator's own
footprint, so a whole-tree reading legitimately exceeds `max_rss_mb` on a healthy multi-process
run. `getrusage` is the correct, and only correct, primitive for this ceiling.

**Breach-counting policy -- an Agent Recommendation (judgment call), not SPEC-mandated.** §12.22's
own literal criterion text says only that RSS "keeps under the ceiling... throughout" and that a
fault-injected variant must fail (halt); it names no debounce count. §11.3's narrative "3
consecutive breaches" language exists specifically to gate the THIRD check after two rounds of
halving the `cpu_pool`/`subprocess` semaphores -- a backoff step D110 (`docs/INTEGRATION_HONESTY.
md`) records as deliberately deferred (no resize-capable primitive exists for either semaphore).
Without the halving step in between, "3 consecutive breaches" has no independent justification as
a standalone debounce for this simpler sample→compare→halt path: it would just delay a halt by two
sample intervals for no corresponding backoff action taken in between. This sampler HALTS ON THE
FIRST over-ceiling sample -- the more conservative reading the brief itself flags as the
defensible default, and the one consistent with the criterion's own literal "keeps RSS under the
ceiling... throughout" (any measured excursion above the ceiling is itself the violation).

**Lifecycle -- the real architectural decision this task's brief left to investigation.**
`RunContext` (`orchestrator/context.py`) is a **frozen** dataclass assembled once per
`PhaseRunner(...)` call site in `cli.py`, and `PhaseRunner` itself is likewise constructed fresh
at every one of those same four call sites (`_run_scan_wave`/`_run_transform_wave`/
`_run_build_wave`/`_run_verify_wave`) -- one wave, one `RunContext`, one `PhaseRunner`, one
`Projector`, all torn down together in that composition root's own `try/finally`
(`_close_wave_projector`). There is no larger "whole run" lifetime object anywhere below the CLI
command boundary itself: a `fleet transform`/`fleet build`/`fleet verify` invocation loops over
MULTIPLE waves, reconstructing `RunContext`+`PhaseRunner`+`Projector` on every iteration, and
`_continue_impl` chains separate phase composition roots end to end rather than sharing one. Given
that, this sampler's lifecycle is scoped to the SAME granularity those three objects already use
-- one `HostMemorySampler` per composition-root call, started before `PhaseRunner.run_wave()` and
stopped after, in each of the four `_run_*_wave` functions in `cli.py`. This is not a new lifecycle
shape: it is the existing `Projector.start()`/`_close_wave_projector` idiom, used a fifth time, for
a fifth per-wave collaborator -- rather than inventing a new run-lifetime singleton (which would
mean either a mutable field forced onto `RunContext`'s frozen dataclass via
`object.__setattr__`, matching no existing precedent there other than the two ALREADY-derived
fields it documents as exceptions, or a new module-global the CLI drives across phase boundaries,
which is exactly the shape CLAUDE.md Guardrail 3 asks orchestration state not to take). Scoping to
`PhaseRunner`'s own constructor/lifecycle instead of the composition root was the other option the
brief named; it was rejected because `PhaseRunner.__init__` already accepts `resource_guard` as a
bare synchronous `Callable` with no async lifecycle of its own (existing tests construct
`PhaseRunner(..., resource_guard=some_lambda)` directly, with no `start`/`stop` step), and giving
`PhaseRunner` an async `start`/`stop` pair to own only for this one collaborator would touch
`PhaseRunner.__init__`/`run_wave()`'s signature and every existing call site for no benefit over
handing it an already-running guard the same way `resource_guard` is already handed in --
`sampler.guard` is a plain zero-arg callable, `resource_guard`'s exact declared type.

**Why the wave-scoped choice still satisfies "sample every second, throughout" for the SPEC's own
literal test scenario.** Indexing ONE synthetic large repo is a single SCAN wave: one
`_run_scan_wave` call, one `HostMemorySampler` alive for that call's whole duration, ticking on its
own `asyncio.Task` concurrently with `PhaseRunner.run_wave`'s per-repo `TaskGroup` -- independent
of whether `_drive`'s own poll site (`reason = self._guard()`, checked once per repo-dispatch
attempt) has any dispatch boundary to poll at *inside* that one long call. The sampler's own tick
loop does not depend on that call site at all; it runs on its own clock regardless of what the one
in-flight dispatch is doing. What this task does NOT change is `_drive`'s poll site itself --
`resource_guard()` is still read only once per repo-dispatch attempt, so a breach that both starts
and clears strictly *within* one uninterrupted dispatch call, before the NEXT poll, would still not
be observed by that call site even though the sampler's own internal state was briefly correct.
Flagged here rather than silently left implicit: closing that residual timing gap, if the
adversarial proof (task 28 / B3) finds it load-bearing, is a change to `_drive`'s own poll
frequency/shape, which this task's brief scoped OUT ("no downstream changes needed... the
halt/exit-5 plumbing already works").
"""

from __future__ import annotations

import asyncio
import resource
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Final
from uuid import UUID

from fleet.orchestrator.runner import HaltReason
from fleet.sandbox.containerstats import ContainerStatsReader
from fleet.util.cgroup import read_process_tree_memory_bytes

__all__ = ["DEFAULT_SAMPLE_INTERVAL_S", "HostMemorySampler", "read_own_rss_bytes"]

#: §11.3's narrative production sampling cadence. §12.22's literal criterion text demands "every
#: second" for the test scenario instead -- neither number is hardcoded into the sampler itself;
#: this is only `HostMemorySampler.__init__`'s default, the same shape `resource_guard` itself is
#: injected in, so a caller (test or production) states its own interval explicitly.
DEFAULT_SAMPLE_INTERVAL_S: Final = 30.0

_BYTES_PER_MB: Final = 1024 * 1024


def read_own_rss_bytes() -> int:
    """The orchestrator process's OWN peak resident-set size, via `resource.getrusage
    (resource.RUSAGE_SELF).ru_maxrss` -- task 29's pure, injectable primitive for `budgets.
    max_rss_mb`, the same DI-seam shape as `util/cgroup.py`'s `read_process_tree_memory_bytes`
    (a bare zero-arg callable a test can substitute wholesale, not a class to subclass).

    **Unit gotcha, verified rather than assumed.** `ru_maxrss` is platform-dependent: Linux
    reports KIBIBYTES, BSD/macOS report bytes. This codebase's only confirmed deployment target
    is Linux (`util/cgroup.py`'s module docstring makes the same assumption explicitly for cgroup
    v2), so this function always multiplies by 1024 -- verified empirically in this task's own
    development sandbox (a 50 MiB allocation moved `ru_maxrss` by ~51200, matching `/proc/self/
    status`'s `VmHWM` in kB to within measurement noise), not merely read off the `getrusage(2)`
    man page. `budgets.max_rss_mb` is expressed in MiB per its own name; the multiplication here
    converts to bytes, matching `_BYTES_PER_MB`'s own unit (MiB, despite the name) so
    `HostMemorySampler`'s ceiling comparison is byte-for-byte consistent across both ceilings.
    """
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


class HostMemorySampler:
    """Ticks on its own `asyncio.Task`, independent of repo-dispatch activity, and exposes
    `guard()` -- a synchronous zero-arg callable matching `PhaseRunner`'s declared
    `resource_guard: Callable[[], HaltReason | None]` type exactly, so a caller wires it in with
    `PhaseRunner(..., resource_guard=sampler.guard)`, no adapter needed.

    **State holder: a plain mutable attribute, not a lock.** This is asyncio -- one thread, one
    event loop. `_tick()` writes `self._host_breached`/`self._own_breached` each in a single
    statement with no `await` between reading a ceiling's source(s) and writing that flag's
    *final* value (the host leg's two reads ARE separately awaited, but nothing observes
    `_host_breached` in between; each flag is only ever assigned once per tick, after its own
    reading(s) are in hand), and `guard()` reads both in a single expression with no `await` at
    all. A reader task and a writer task can only interleave at an `await` point, and there is no
    `await` on either side of either assignment or of `guard()`'s read, so no lock is needed for
    correctness here -- unlike e.g. `CostLedger`, which guards durable, multi-step read-modify-write
    sequences a lock genuinely protects.
    """

    def __init__(
        self,
        *,
        run_id: UUID | str,
        max_host_rss_mb: int,
        max_rss_mb: int,
        interval_s: float = DEFAULT_SAMPLE_INTERVAL_S,
        cgroup_reader: Callable[[], int] = read_process_tree_memory_bytes,
        rss_reader: Callable[[], int] = read_own_rss_bytes,
        container_reader: ContainerStatsReader | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._run_id = run_id
        self._host_ceiling_bytes = max_host_rss_mb * _BYTES_PER_MB
        self._own_ceiling_bytes = max_rss_mb * _BYTES_PER_MB
        self._interval_s = interval_s
        self._cgroup_reader = cgroup_reader
        self._rss_reader = rss_reader
        #: Defaulted here (not at the module level) so a production caller that supplies nothing
        #: gets the REAL reader (real `docker` CLI, over `fleet.util.proc.run`) -- and a test that
        #: forgets to override this constructs a real reader too, which is exactly the fail-loud
        #: outcome wanted rather than a silently-fake default.
        self._container_reader = (
            container_reader if container_reader is not None else (ContainerStatsReader())
        )
        self._sleep = sleep
        self._host_breached = False
        self._own_breached = False
        self._task: asyncio.Task[None] | None = None

    def guard(self) -> HaltReason | None:
        """`PhaseRunner`'s `resource_guard()`. Never awaits, never raises -- see class
        docstring for why the plain attribute read is safe. A sampler `_tick()` failure (the
        cgroup/container/rss reader raised) does NOT surface here: it would be misattributed as
        an unrelated single repo's failure by `PhaseRunner._isolated`'s blanket `except
        Exception`, which is the wrong place for a host-level infrastructure failure to be
        blamed. It surfaces instead when the caller `stop()`s this sampler at wave teardown
        (below), OUTSIDE `PhaseRunner.run_wave()`'s per-repo isolation boundary, where a
        re-raised reader failure correctly fails the whole wave rather than one repo (Rule 11:
        fail loud, but at the right scope).

        A breach of EITHER ceiling halts -- task 29's brief is explicit that the `HaltReason`
        enum member and exit code stay `HOST_MEMORY`/5 for both (task 26/27 confirmed no other
        variant exists or is warranted); `breach_kinds` below is where the two are told apart for
        a caller that wants to say which."""
        return HaltReason.HOST_MEMORY if (self._host_breached or self._own_breached) else None

    def breach_kinds(self) -> frozenset[str]:
        """Which ceiling(s) the LAST completed tick found breached -- `"host_rss"` (the whole
        process tree plus containers, `max_host_rss_mb`), `"own_rss"` (the orchestrator's own
        peak RSS, `max_rss_mb`), both, or neither (empty). `guard()` itself deliberately does not
        distinguish (see its docstring); this is the diagnostic detail a caller MAY read
        alongside it -- e.g. for a future log line -- without changing `guard()`'s own contract
        or the `HaltReason`/exit-code plumbing task 29's brief pins to `HOST_MEMORY`/5 for both.
        """
        kinds: set[str] = set()
        if self._host_breached:
            kinds.add("host_rss")
        if self._own_breached:
            kinds.add("own_rss")
        return frozenset(kinds)

    async def _tick(self) -> None:
        """One sample, both ceilings, independently:

        * whole process tree (task 26's cgroup reader) plus every running `fleet-<run_id>-*`
          container (task 26's `ContainerStatsReader`), compared against `max_host_rss_mb`.
        * the orchestrator's own peak RSS (`rss_reader`, `resource.getrusage` by default),
          compared against `max_rss_mb` -- task 29's addition; see module docstring for why
          `getrusage` is the correct primitive for THIS ceiling specifically.

        `>=`, not `>`, for both: "keeps RSS under the ceiling" (§12.22) reads a sample sitting
        exactly AT the ceiling as not under it."""
        tree_bytes = self._cgroup_reader()
        container_total = await self._container_reader.read_total(self._run_id)
        total_bytes = tree_bytes + container_total.total_bytes
        self._host_breached = total_bytes >= self._host_ceiling_bytes
        own_bytes = self._rss_reader()
        self._own_breached = own_bytes >= self._own_ceiling_bytes

    async def _run_forever(self) -> None:
        """Tick immediately on start (so `guard()` reflects a real reading as soon as possible
        rather than only after the first full interval elapses), then every `interval_s`
        thereafter, forever -- cancelled from outside by `stop()`. A `_tick()` exception (the
        underlying reader raised `CgroupUnavailableError`/`ContainerStatsUnavailableError`) ends
        this loop and is stored on the task; it is deliberately NOT caught here, so it propagates
        when `stop()` awaits the task below rather than being silently absorbed -- an unreadable
        host is not the same fact as "the host has room," and Rule 11 forbids collapsing those."""
        while True:
            await self._tick()
            await self._sleep(self._interval_s)

    @property
    def is_running(self) -> bool:
        """Whether a background task is currently owned by this sampler -- `True` from `start()`
        until the matching `stop()` returns (even if `stop()` is about to re-raise a reader
        failure; see `stop()`'s docstring, `self._task` is cleared before that re-raise so a
        caller's own cleanup can tell the sampler is no longer running)."""
        return self._task is not None

    def start(self) -> None:
        """Starts the background task. Not re-entrant: calling this twice without an
        intervening `stop()` is a caller bug (two ticking tasks racing to write the same
        `_host_breached`/`_own_breached` flags), raised loudly rather than silently replacing
        the first task and leaking it un-cancelled."""
        if self._task is not None:
            raise RuntimeError(
                "HostMemorySampler.start() called while a sampler task is already running "
                "-- call stop() first"
            )
        self._task = asyncio.create_task(
            self._run_forever(), name=f"host-memory-sampler-{self._run_id}"
        )

    async def stop(self) -> None:
        """Cancels and awaits the background task -- mirrors `PhaseRunner._dispatch`'s own
        `heartbeat.cancel(); with suppress(asyncio.CancelledError): await heartbeat` shape
        exactly, the one existing periodic-task-teardown pattern in this codebase. A no-op if
        `start()` was never called or `stop()` already ran (idempotent, so a caller's own
        `finally` need not track whether it already cleaned up). Re-raises any NON-cancellation
        exception the task ended with -- see `guard()`'s docstring for why that must happen here
        and not inside `guard()` itself."""
        if self._task is None:
            return
        task, self._task = self._task, None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
