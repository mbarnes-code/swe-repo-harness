"""Phase 3 step 4: the sandboxed `bazel build` + `bazel test` (ADR-0010, SPEC §3.3).

Runs over an **immutable snapshot ref**, never over the integration branch. §3.3 step 1 is blunt
about why: a 20-minute `bazel build` reading `integration` can see merges landing mid-build, so a
`BUILD_ERROR` would be a property of scheduling luck rather than of a named tree, and a real
attempt would be consumed by a race. `BuildverifyInput.integration_ref` is therefore required, is
required to be a `refs/…` ref rather than a branch name, and is echoed on the output as the tree
this verdict describes.

**The exit code is the verdict** (§3.3). The model is never asked whether the build passed; it is
asked — on rungs 2 and 3 only, and only through `Role.BUILD_DIAGNOSIS` — what the *verbatim*
stderr means, and its answer is recorded as advice beside the mechanical classification, never in
place of it (Rule 5, Constraint 5).

**"No tests" is not a failure.** Every exit code this worker classifies was reproduced against the
vendored Bazel (see the table above `classify_build_failure`), and the row that matters most at
fleet scale is exit 4, `NO_TESTS_FOUND`: a library with no tests of its own is ordinary, and
treating it as a build failure spends three LLM-bearing rungs and then demands human triage for a
repo that built perfectly. The single case where an empty test set IS a failure is §12.11's
green-and-empty — `repos.baseline_test_count > 0` and nothing left to run — which is why
`BuildverifyInput.baseline_test_count` exists and why the two are distinguished by data rather
than by hope.

**A failure is persisted, not rejected.** The whole reason `WorkerError.stderr_tail` is a
`TruncatedStr` and not a `max_length` string is that a 400 KB Gradle/Bazel stderr failing
validation is an attempt that never reaches SQLite, an `attempts` counter that never increments,
and a repair loop re-running the identical failing build forever. So the tail is truncated, the
FULL stream stays on disk, and `WorkerError.artifact_ref` carries that path (§11.3, §5 invariant 3).

**Nothing here needs `bazel`, `docker`, or a network to be tested.** Every process is launched
through an injected `CommandRunner` (`util/proc.py`, CLAUDE.md guardrail 3), so a test asserts on
the argv that *would* have run. `LoggedRunner` — the default — is that seam bound to a log
directory, and it is shared with `rdepverify.py`, which needs the same "full stream on disk"
property for a closure that is megabytes wide.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import ClassVar, Final, Literal

from pydantic import Field, field_validator

from fleet.llm.calls import Evidence, diagnose_build
from fleet.llm.client import LlmError
from fleet.models.base import FleetModel
from fleet.models.enums import FailureClass, Phase
from fleet.models.tasks import TokenUsage
from fleet.orchestrator.registry import register_worker
from fleet.sandbox.container import (
    DEFAULT_CPUS,
    DEFAULT_MEMORY,
    DEFAULT_NETWORK,
    ContainerSandbox,
    ContainerSpec,
    Mount,
    current_user_spec,
    docker_run_argv,
    spec_for_attempt,
)
from fleet.sandbox.worktree import sandbox_name
from fleet.util.fs import DiskFloorBreached, require_free_space
from fleet.util.proc import CommandRunner, ProcResult, run
from fleet.workers.base import (
    BaseWorker,
    WorkerBudget,
    WorkerContext,
    WorkerError,
    WorkerInput,
    WorkerOutput,
    WorkerResult,
    WorkerStatus,
    loop_now,
)

__all__ = [
    "BUILD_UNIT",
    "CACHE_MOUNT_ROOT",
    "C_TOOLCHAIN_PROBE",
    "C_TOOLCHAIN_PROBE_TIMEOUT_S",
    "INFRA_EXIT_CODES",
    "NO_TESTS_FOUND",
    "OOM_EXIT_CODES",
    "TEST_UNIT",
    "UNREPEATABLE_EXIT_CODES",
    "BuildverifyInput",
    "BuildverifyOutput",
    "BuildverifyWorker",
    "CacheMount",
    "LoggedRunner",
    "StepRecord",
    "classify_build_failure",
    "dirs_present",
    "error_from_proc",
    "files_present",
    "no_test_targets",
]


BUILD_UNIT: Final = "build"
TEST_UNIT: Final = "test"

OOM_EXIT_CODES: Final[frozenset[int]] = frozenset({137, -9})
"""`SIGKILL` from the cgroup OOM killer. Mechanically distinguishable from exit 1, which is the
whole reason `WorkerError.retryable` is a flag and not a message match: 1 is a repair prompt and
137 is a re-queue at a lower `--jobs`, and no string comparison tells them apart."""

_COMMAND_NOT_FOUND: Final = 127
"""`bazel` (or `docker`) is absent from the image. Re-running an identical rung cannot install it,
so this is one of the build failures the worker marks non-retryable on mechanical evidence."""

_DOCKER_CANNOT_RUN: Final = 125
"""`docker run` itself failed — the CONTAINER never started, so nothing inside the image ran and
no exit code from it exists. The case this harness meets is an image that is not present locally
and cannot be pulled (`Unable to find image '<ref>' locally` followed by a registry error), which
is exactly what happens when nobody has built `docker/fleet-build.Dockerfile` on this host.

Mechanically distinct from every other code a probe can return, and worth distinguishing because
the two verdicts point at different files: 125 is "build the image" (or fix
`settings.verify.container_image`), while a non-zero code from INSIDE the image is "the image is
wrong". Reported non-retryable for the same reason as 127 — re-running an identical rung cannot
build an image any more than it can install a compiler."""

C_TOOLCHAIN_PROBE: Final = (
    "cc=${CC:-}; "
    'cc=${cc#"${cc%%[![:space:]]*}"}; cc=${cc%"${cc##*[![:space:]]}"}; '
    'case "$cc" in '
    '"") command -v gcc ;; '
    '/*) [ -x "$cc" ] ;; '
    '*) command -v "$cc" ;; '
    "esac"
)
"""The same question Bazel's C++ autoconfiguration asks — asked the same WAY, which is the point.

A line-by-line mirror of `_find_generic(repository_ctx, "gcc", "CC", overridden_tools)`, the ONLY
compiler lookup in `bazelbuild/rules_cc`'s `cc/private/toolchain/unix_cc_configure.bzl` (where the
C++ autoconfiguration has lived since Bazel 8; `find_cc` calls it, `cc_configure_extension`
generates `local_config_cc` from the result). Upstream resolves, in order: `overridden_tools` (not
reachable from an image — nothing in this harness passes any), then `CC` from the environment —
`.strip()`ped, and if what remains is non-empty it *replaces* the default rather than
supplementing it — then the literal name `"gcc"`, then `repository_ctx.which(result)`. An
ABSOLUTE value short-circuits `which` and is returned as-is. Nothing found ⇒
`auto_configure_fail("Cannot find gcc or CC…")`.

So each clause below is a clause up there: the two `${cc#…}`/`${cc%…}` expansions are Starlark's
`.strip()`; `""` ⇒ `command -v gcc` is the default name reaching `which`; `/*` is the absolute
short-circuit; the last arm is a relative `$CC` reaching `which`.

**What this deliberately does NOT ask for.** `cc` and `clang` are NOT lookup candidates — `cc` is
never searched at all, and `clang` appears upstream only inside `_is_clang(...)`, which classifies
a binary already found by the lookup above. The previous three-name probe (`command -v cc ||
command -v gcc || command -v clang`) was wrong in both directions: it passed a clang-only image
(or one where `cc` is a symlink to clang and no `gcc` exists) that Bazel then refuses, and it
refused a working image carrying `ENV CC=/opt/toolchain/bin/gcc` or `ENV CC=gcc-13`.

**One deliberate divergence,** in the absolute arm: upstream returns an absolute `CC` unvalidated,
whereas `[ -x "$cc" ]` tests it. Not a stricter gate in practice — the very next thing
`configure_unix_toolchain` does with that path is `repository_ctx.execute([cc, "-E", …])` to read
the builtin include directories — so this asks one step earlier the question the fetch asks anyway.

`command -v` is POSIX `sh`, so no `which(1)` need exist in the image; the shell is the only
assumption, and an image without one fails the probe with 127 — the same verdict for the same
reason.
"""

C_TOOLCHAIN_PROBE_TIMEOUT_S: Final = 120.0
"""Generous for `command -v`, and it is not sized for that: this is the first `docker run` of the
attempt, so it also pays the image pull. It can only TIGHTEN `ctx.deadline` (`util/proc.run`
takes the earlier of the two), never extend it."""

# --------------------------------------------------------------------------------------
# Bazel's exit codes, verified against the vendored binary (`tools/bin/bazel`, Bazel 9.2.0)
# rather than quoted from memory. Every row below was reproduced:
#
#   | code | Bazel name                    | reproduced by                              |
#   |------|-------------------------------|--------------------------------------------|
#   |  0   | SUCCESS                       | `bazel test` over a passing `sh_test`      |
#   |  1   | BUILD_FAILURE                 | a failing `genrule`; a loading error; an   |
#   |      |                               | unknown package — under `test` too         |
#   |  2   | COMMAND_LINE_ERROR            | `--not_a_real_flag`; an unknown verb       |
#   |  3   | TESTS_FAILED                  | one `sh_test` exiting 1                    |
#   |  4   | NO_TESTS_FOUND                | `bazel test` over a package of `genrule`s  |
#   |  8   | INTERRUPTED                   | SIGINT to the client mid-build             |
#   |  9   | LOCK_HELD_NOBLOCK_FOR_LOCK    | a second `bazel` on one output base        |
#   | 36   | LOCAL_ENVIRONMENTAL_ERROR     | an unwritable `--output_user_root`         |
#
# The distinction the classifier is built on: 1 DOMINATES 4. A `bazel test` whose build failed
# exits 1 and prints "No test targets were found" as well — so exit 4 is emitted only when the
# build itself completed successfully. That is what makes "exit 4 ⇒ the tree is fine, there was
# simply nothing to run" a mechanical fact rather than an inference from the message text (§3.3:
# the exit code is the verdict).
# --------------------------------------------------------------------------------------

NO_TESTS_FOUND: Final = 4
"""`bazel test` found no test targets under the pattern, and the build that preceded it was green.

Ordinary at fleet scale: a library with no tests of its own is not a broken repo, and §3.3's
success criterion is `bazel build` exit 0 *and* `bazel test` exit 0 — a criterion an empty test
set cannot meet and was never about. Before this constant existed, exit 4 fell through to a
retryable `TEST_FAILURE`, burned all three ADR-0014 rungs on a repo that built perfectly and
escalated it to `REQUIRES_HUMAN_INTERVENTION`.
"""

INFRA_EXIT_CODES: Final[frozenset[int]] = frozenset({8, 9, 36}) | OOM_EXIT_CODES
"""Bazel said the *environment* failed, not the repo: interrupted (8, which is what §3.4's wave
drain leaves behind when it kills a build), another command holding the output-base lock (9),
and a local environmental error such as an unwritable output root (36). ADR-0014 spends no
attempt on any of them — the fleet's own clock and disk may not consume a repo's three chances."""

UNREPEATABLE_EXIT_CODES: Final[frozenset[int]] = frozenset({2, _COMMAND_NOT_FOUND})
"""Argv the next rung would submit byte-identically: a malformed flag or verb (2) and a missing
binary (127). A repair prompt cannot fix the harness's own command line, so these terminate
without charging the ladder instead of failing three times to learn the same thing."""


CACHE_MOUNT_ROOT: Final = "/cache"
"""Where every shared Bazel cache is bind-mounted inside the sandbox. One constant, because the
mount target and the `--*_cache=` value it feeds are the same string by construction."""

_CACHE_FLAGS: Final[Mapping[str, str]] = {
    "disk": "--disk_cache",
    "repository": "--repository_cache",
}
"""role → the bazel flag that points at it. Both were checked against the vendored binary rather
than recalled: `bazel canonicalize-flags --for_command=build|test -- --disk_cache=… ` accepts both
on Bazel 9.2.0, and repeats collapse to the LAST occurrence (see `CacheMount.flag`)."""


class CacheMount(FleetModel):
    """One shared Bazel cache: the host directory to mount, and the flag that tells bazel it exists.

    **The two used to be independent, and the mounts were therefore inert.** `verify.disk_cache`
    and `verify.repository_cache` were bind-mounted read-write into the verify container, but the
    argv named neither, so bazel never looked at either directory. Under `verify.network = "none"`
    that is not a lost cache hit but a build that cannot resolve a single module: a cold container
    has no repository cache to fetch `go_sdk.download` or any BCR `bazel_dep` from, and no network
    to fall back to. The fix is this type: the mount TARGET and the flag VALUE are both derived
    from `path` here, so a rename of one is a rename of the other and they cannot drift.

    `role` rather than a bare path list because the two flags are not interchangeable and nothing
    in a host path reliably says which is which — `_cache_mounts` in `cli.py` used to convey that
    by ORDER, which is exactly the kind of convention that survives until someone appends a third
    directory.
    """

    role: Literal["disk", "repository"]
    path: str = Field(
        min_length=1,
        description="Absolute HOST path, created by the caller. Mounted read-write and shared "
        "across containers and attempts (§3.4 bounds table).",
    )

    @field_validator("path")
    @classmethod
    def _named_directory(cls, value: str) -> str:
        """Normalise and require a final component: `target` is built from it, and `/cache/` is
        not a directory anything can mount to."""
        normalised = str(Path(value))
        if not Path(normalised).name:
            raise ValueError(f"cache mount path {value!r} has no final path component")
        return normalised

    @property
    def target(self) -> str:
        """The CONTAINER-side path: `/cache/<name>`, the bind mount's target."""
        return f"{CACHE_MOUNT_ROOT}/{Path(self.path).name}"

    def mount(self) -> Mount:
        """The bind mount. Read-write on purpose (§3.4): ADR-0010's read-only mount is the
        *toolchain* cache, and a cache bazel cannot write to is a cache that never fills."""
        return Mount(source=Path(self.path), target=self.target)

    def flag(self, *, sandboxed: bool) -> str:
        """`--disk_cache=…` / `--repository_cache=…` for the side bazel actually runs on.

        **Sandboxed and unsandboxed are different paths and getting it backwards is silent.**
        Inside the container only `target` exists; on the host run (`image is None` — what
        `fleet build --no-sandbox`, every offline test and every host-side dry run take) only
        `path` does. A host path handed to the container names a directory that is not mounted
        there, so bazel would create it inside the container's own filesystem and `--rm` would
        delete it; a container path handed to the host would write the fleet's caches to
        `/cache`. Neither fails loudly. Both throw the cache away.
        """
        return f"{_CACHE_FLAGS[self.role]}={self.target if sandboxed else self.path}"


def no_test_targets(result: ProcResult, *, unit: str) -> bool:
    """Did this step exit 4 — build green, nothing to test?

    Narrow on purpose. Only the `test` step can report it (`BUILD_UNIT` never runs tests, and
    `rdepverify`'s `rdeps_test` unit has its own closure semantics this worker does not own), and
    a timed-out or never-started process has no exit code worth reading.
    """
    return (
        unit == TEST_UNIT
        and result.started
        and not result.timed_out
        and result.exit_code == NO_TESTS_FOUND
    )


def dirs_present(*paths: Path) -> bool:
    """Are all of these directories on disk?

    A *sync* helper deliberately called from `async def preconditions_hold`: a handful of
    `stat(2)` calls do not need a thread, and writing them inline in a coroutine trips `ASYNC240`,
    a rule aimed at trio's blocking-IO discipline. Naming the check once also makes the refusal
    readable — "the worktree the checkpoint describes is gone" rather than three `Path` calls.
    """
    return all(path.is_dir() for path in paths)


def files_present(*paths: Path) -> bool:
    """Are all of these files on disk? Same reasoning as `dirs_present`."""
    return all(path.exists() for path in paths)


class LoggedRunner:
    """`util.proc.run` bound to a log directory — the FULL stream stays on disk (§11.3).

    A class rather than a `functools.partial` for two reasons: `CommandRunner` is a `Protocol`
    with keyword-only parameters, so a structural implementation is checkable by `mypy --strict`;
    and an *instance* is not a descriptor, so holding one in a worker's slot never turns into an
    accidentally-bound method the way a bare function assigned to a class attribute does.

    The log directory is what makes `bazel/query.query_stdout` legal at all: a 40 000-target
    closure is megabytes, `stdout_tail` is capped at 32 KiB, and reading the tail would silently
    drop most of the blast radius and report the remainder as the whole of it.
    """

    __slots__ = ("log_dir", "stem")

    def __init__(self, log_dir: Path | str, stem: str = "proc") -> None:
        self.log_dir = Path(log_dir)
        self.stem = stem

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        deadline: float | None = None,
        timeout_s: float | None = None,
    ) -> ProcResult:
        return await run(
            argv,
            cwd=cwd,
            env=env,
            deadline=deadline,
            timeout_s=timeout_s,
            log_dir=self.log_dir,
            log_stem=self.stem,
        )


def classify_build_failure(result: ProcResult, *, unit: str) -> tuple[FailureClass, bool]:
    """`(failure_class, retryable)` from mechanical evidence only — never from the message.

    `TEST_FAILURE` and `BUILD_ERROR` are different rows in §13 and different repair prompts, so
    the step that failed decides the class; the exit code decides retryability. The exit-code
    table above this function is what the branches below read, and every row in it was reproduced
    against the real binary — a classifier built on remembered exit codes is a classifier that
    escalates repos for reasons nobody can reproduce.
    """
    if result.timed_out:
        return FailureClass.TIMEOUT, True
    if not result.started:
        return FailureClass.TRANSIENT_INFRA, True
    if result.exit_code in INFRA_EXIT_CODES:
        # The container hit its memory cap, the fleet interrupted the build, another command held
        # the lock, or the output base was unwritable. None of those is the repo's failure, and
        # none is an attempt worth spending on the same rung (§11.8 owns the re-queue).
        return FailureClass.TRANSIENT_INFRA, True
    if result.exit_code in UNREPEATABLE_EXIT_CODES:
        return FailureClass.BUILD_ERROR, False
    if no_test_targets(result, unit=unit):
        # Reached ONLY when the caller has already decided this empty test set is the §12.11
        # regression — a repo whose `baseline_test_count` was positive and whose migrated package
        # now declares no test target at all. `BuildverifyWorker.run` returns success for exit 4
        # otherwise, so this branch never sees a library that legitimately has no tests. The class
        # is `BUILD_ERROR` and not `TEST_FAILURE` because no test failed: a `filegroup` was emitted
        # where a `*_test` belonged, and the repair rung that can fix that is the one that
        # regenerates `BUILD.bazel`.
        return FailureClass.BUILD_ERROR, True
    return (FailureClass.TEST_FAILURE if unit == TEST_UNIT else FailureClass.BUILD_ERROR), True


def error_from_proc(
    result: ProcResult, *, unit: str, extra: str = ""
) -> WorkerError:
    """The structured failure for a non-zero build, with the evidence a repair prompt needs.

    `stderr_tail` is handed the tail verbatim and TRUNCATED by `TruncatedStr` — never rejected —
    and `artifact_ref` is the path to the whole stream, which is what the ADR-0021 repair rung
    reads (Constraint 5). Both, because the tail is what a human skims and the file is what the
    next attempt is prompted with.
    """
    failure_class, retryable = classify_build_failure(result, unit=unit)
    tail = result.stderr_tail or result.stdout_tail
    return WorkerError(
        failure_class=failure_class,
        retryable=retryable,
        exit_code=result.exit_code,
        stderr_tail=f"{extra}{tail}" if extra else tail,
        artifact_ref=None if result.stderr_path is None else str(result.stderr_path),
    )


class StepRecord(WorkerOutput):
    """One `bazel` invocation as evidence: exactly the columns an `attempts` row needs (§3.3).

    Deliberately carries no timestamps. A worker stamps none (§5 invariant 4) — `utcnow()` is the
    orchestrator host's clock, and `duration_ms` is elapsed time measured by `util/proc.py`, which
    is not a timestamp at all.
    """

    unit: str
    command: list[str] = Field(default_factory=list)
    exit_code: int
    duration_ms: int = Field(default=0, ge=0)
    timed_out: bool = False
    ok: bool = False
    log_path: str | None = None
    stdout_log_path: str | None = None


class BuildverifyInput(WorkerInput):
    """What one sandboxed verification needs. `integration_ref` is not optional, on purpose."""

    dest: str = Field(min_length=1, description="Monorepo-relative package dir; layout() output")
    integration_ref: str = Field(
        min_length=1,
        description="The IMMUTABLE `refs/fleet/<run_id>/integration/<seq>` this build reads "
        "(§3.3 step 1). A branch name here is the race the snapshot exists to remove.",
    )
    worktree: str | None = Field(
        default=None, description="Defaults to `ctx.workdir` — the worktree cut from the snapshot"
    )
    log_dir: str = Field(
        default="artifacts/logs", description="Root of the full-stream logs; `<log_dir>/<run_id>/`"
    )
    bazel_bin: str = "bazel"
    run_tests: bool = Field(
        default=True,
        description="False for a CONTRACT node: an IDL package has no tests of its own, and its "
        "real test is that later waves compile against the regenerated bindings (§3.3)",
    )
    baseline_test_count: int = Field(
        default=0,
        ge=0,
        description="`repos.baseline_test_count` — the repo's NATIVE, pre-migration test count. "
        "The ONLY thing that separates 'this library never had tests' (0, which is also what a "
        "run with `baseline_ok IS NULL` back-fills to, i.e. 'unknown') from 'this migration "
        "DELETED the repo's tests' (>0 with an empty migrated test set), which §12.11 refuses.",
    )
    jobs: int | None = Field(default=None, description="Bounded by the container CPU cap (§3.4)")
    keep_going: bool = True
    build_event_json: bool = Field(
        default=True, description="§3.4: FailureClass comes from the BEP, not from regexing stderr"
    )
    extra_args: list[str] = Field(default_factory=list)
    image: str | None = Field(
        default=None,
        description="Sandbox image. When set the bazel command is wrapped in a `--network=none` "
        "container (ADR-0010); when None the command runs directly, which is what an offline "
        "test and a host-side dry run both need.",
    )
    container_workdir: str = "/work"
    container_memory: str = DEFAULT_MEMORY
    container_cpus: str = DEFAULT_CPUS
    container_network: str = DEFAULT_NETWORK
    cache_mounts: list[CacheMount] = Field(
        default_factory=list,
        description="`verify.disk_cache`/`verify.repository_cache`. Each entry is BOTH a "
        "read-write bind mount and the `--disk_cache=`/`--repository_cache=` flag that points "
        "bazel at it — one object, so the mount and the flag cannot name different directories "
        "(§3.4 bounds table). An empty list emits no flag and mounts nothing.",
    )
    min_free_bytes: int = Field(
        default=0,
        ge=0,
        description="`preflight.min_free_bytes`, re-checked before THIS build (§11.3). A Bazel "
        "invocation is the other operation that consumes gigabytes without asking — the disk "
        "cache, the action cache and an `external/` tree per output base. `0` disables the gate "
        "and is the default only for a payload built by hand; `fleet build`/`fleet verify` pass "
        "the configured value.",
    )


class BuildverifyOutput(WorkerOutput):
    """The checkpoint: which tree was built, what ran, and what each step exited with."""

    dest: str
    integration_ref: str = Field(
        description="`attempts.integration_ref` (§3.3): the immutable tree this verdict is about"
    )
    build_ok: bool = False
    test_ok: bool = False
    tests_ran: bool = False
    no_test_targets: bool = Field(
        default=False,
        description="`bazel test` exited 4: the build was green and the package declares no test "
        "target. Its OWN recorded outcome, neither a pass nor a failure — `test_ok` stays False "
        "because nothing passed, `tests_ran` stays False because nothing ran, and this flag is "
        "the difference between the two, which §12.11's `(repo, baseline, migrated)` table needs "
        "and a bare `test_ok = False` cannot express.",
    )
    steps: list[StepRecord] = Field(default_factory=list)
    diagnosis: str = Field(
        default="",
        description="`build_diagnosis` prose, advisory only. The exit code is the verdict (§3.3).",
    )
    diagnosis_failure_class: FailureClass | None = Field(
        default=None, description="The model's opinion, recorded beside — never over — the code's"
    )
    baseline_test_count: int = Field(
        default=0,
        ge=0,
        description="Echo of the input, so §12.11's `(repo, baseline, migrated)` table is "
        "readable off the checkpoint without a second join back to `repos`",
    )

    @property
    def tests_lost(self) -> bool:
        """§12.11's green-and-empty failure: the repo had tests natively and the migrated package
        declares none. A `filegroup` emitted where a `*_test` belonged builds clean and "tests"
        clean at zero targets — passing the first half of the §3.3 criterion while deleting the
        repo's entire safety net. That is the one empty test set that IS a failure."""
        return self.no_test_targets and self.baseline_test_count > 0

    @property
    def green(self) -> bool:
        return self.build_ok and (self.test_ok or not self.tests_ran) and not self.tests_lost


@register_worker
class BuildverifyWorker(BaseWorker[BuildverifyInput, BuildverifyOutput]):
    """`bazel build //<dest>/...` then `bazel test //<dest>/...`, inside the sandbox (§3.3 step 4).

    Stateless in the §7.2 sense: `__slots__` means the instance has no `__dict__`, so the two
    collaborators it is constructed with are seams, not per-repo state that could leak from repo A
    into repo B.
    """

    __slots__ = ("_runner",)

    name: ClassVar[str] = "buildverify"
    phase: ClassVar[Phase] = Phase.BUILD
    input_model: ClassVar[type[WorkerInput]] = BuildverifyInput
    output_model: ClassVar[type[WorkerOutput]] = BuildverifyOutput
    budget: ClassVar[WorkerBudget] = WorkerBudget(wall_clock_s=3600, max_subprocesses=4)

    def __init__(self, *, runner: CommandRunner | None = None) -> None:
        """`runner=None` builds a `LoggedRunner` per invocation from `payload.log_dir`, because the
        full stream must reach disk for `WorkerError.artifact_ref` to point at anything. A test
        injects its own recorder and never touches `bazel`.

        The `ModelClient` is deliberately absent: `build_diagnosis` calls `ctx.llm` (§7.1), the one
        call surface, so there is no second place a client could be supplied from — or forgotten.
        """
        self._runner = runner

    # ------------------------------------------------------------------ preconditions

    async def preconditions_hold(self, ctx: WorkerContext, payload: BuildverifyInput) -> bool:
        """Re-entry is admitted only for a build whose *tree* is still nameable and present.

        Three refusals, each a real defect this method exists to catch:
        `integration_ref` that is not a `refs/…` ref is a build against the moving integration
        branch — the §3.3 race whose whole cost is a `BUILD_ERROR` no later phase can attribute;
        a missing worktree means the reaper already removed what the checkpoint describes; and a
        `dest` with no `BUILD.bazel` means Phase 3 step 2 did not land, so re-entering here would
        record a build failure for a file that was never written.
        """
        if not payload.integration_ref.startswith("refs/"):
            return False
        worktree = Path(payload.worktree or ctx.workdir)
        if not dirs_present(worktree):
            return False
        return files_present(worktree / payload.dest / "BUILD.bazel")

    async def _c_toolchain_gate(
        self, ctx: WorkerContext, payload: BuildverifyInput, runner: CommandRunner
    ) -> WorkerError | None:
        """`None` if a C compiler resolves inside the sandbox image; otherwise the refusal.

        **The hidden dependency this makes explicit.** Nothing in this harness mentions `cgo`, and
        nothing needs to: `bazel` cannot ANALYSE a target of any language without a discoverable
        host C compiler. `rules_cc`'s `cc_configure_extension` generates `local_config_cc` by
        looking for `gcc` on `PATH` (or the image's `ENV CC` — see `C_TOOLCHAIN_PROBE`, which
        mirrors that lookup), and `@@rules_go+//:stdlib` — the Go standard library, cgo or not —
        depends on `local_config_cc//:cc-compiler-k8`. Measured, not reasoned about, and pinned by
        `test_the_pure_go_tree_fails_to_analyse_when_no_c_compiler_is_discoverable`: a PURE-Go
        package with no `import "C"` anywhere fails, and with `--keep_going` — which
        `BuildverifyInput.keep_going` DEFAULTS to — Bazel reports `Found 0 targets` and exits
        non-zero. `--nobuild` and `cquery` fail identically; this is not an execution-phase problem
        a flag routes around, and `--network=none` is not the cause (the lookup is local).

        **Where it fails, precisely.** Not "the analysis phase": the lookup runs while the
        `local_config_cc` REPOSITORY is being fetched, at loading time, and its failure is a
        Starlark `auto_configure_fail` — `Auto-Configuration Error: Cannot find gcc or CC…`. The
        `Found 0 targets` / exit 1 above is what an operator then SEES under `--keep_going`, and
        it is downstream of that `fail()`, not the mechanism of it. Both statements are true; only
        one of them is the thing to fix.

        **Why it is a gate and not a classification.** That failure arrives as a bare exit 1, which
        `classify_build_failure` reads as a *retryable* `BUILD_ERROR` — so the missing compiler
        costs all three ADR-0014 rungs (two of them LLM-bearing, prompting a model to repair a
        `BUILD.bazel` that is fine) and then `REQUIRES_HUMAN_INTERVENTION`, with an operator left
        to rediscover `//:stdlib` from a stderr about a repository fetch. Refusing costs one
        `command -v`.

        **Non-retryable, for the same mechanical reason as exit 127** (`_COMMAND_NOT_FOUND`,
        "`bazel` is absent from the image"): re-running an identical rung cannot install a
        compiler any more than it can install `bazel`. `retryable=False` is what makes
        `RetryPolicy.decide` answer `TERMINATE` instead of spending the ladder.

        **Sandboxed runs only.** `payload.image is None` is the host path — an unsandboxed run
        already resolved `bazel` off the same `PATH` the compiler would come from, so a gate there
        would spend a `docker run` to discover what the very next command discovers for free, and
        would fail every offline/dry-run test that has no daemon at all. The image is the case
        where the contents are unknown AT RUNTIME. There IS a Dockerfile now —
        `docker/fleet-build.Dockerfile`, which installs `gcc`+`libc6-dev` for exactly this
        lookup, and `test_sandbox.test_the_fleet_build_image_satisfies_the_c_toolchain_probe`
        runs this very probe against the built image — but neither fact is checkable from here:
        `settings.verify.container_image` is operator-configurable, the tag it names is a LOCAL
        one that this host may never have built, and a locally-built tag can be older than the
        Dockerfile. So the probe still asks rather than assumes; what changed is that its
        refusal now names a file an operator can act on.

        **A 125 is not a missing compiler** (`_DOCKER_CANNOT_RUN`). `docker run` returns it when
        the container never STARTED — most often the image is absent locally and unpullable — so
        the probe never executed and the image's contents remain untested. Both verdicts are
        non-retryable, but they send an operator to different files, so they are reported
        separately.

        **Known limit — a compiler is the floor, not sufficiency.** Gazelle emits `cgo = True`
        automatically for any package containing `import "C"`, so PASS 4 will faithfully publish
        cgo targets; those additionally need the headers and system libraries their `#cgo`
        directives name, and the corpus's cgo repos reference `ole32`, `crypt32`, `IOKit` and
        `sqlite3` — none of which a minimal image carries and none of which this probe looks for.
        Those failures still arrive as ordinary per-repo `BUILD_ERROR`s from the build itself.
        This gate removes exactly one case: the one where NOTHING analyses.

        **Not in `preconditions_hold`.** That method is consulted only by `PhaseRunner._re_entry`,
        only when a checkpoint already exists, and its `False` means "the tree is unusable, re-run
        the phase from `base_ref`" — a verdict that would send a compiler-less image around the
        loop again rather than stopping. This gate is a refusal with evidence, so it belongs where
        the other one that costs the run its attempt does: beside `require_free_space`, before the
        first invocation.
        """
        if payload.image is None:
            return None
        spec = ContainerSpec(
            image=payload.image,
            # NOT `spec_for_attempt`'s name: `on_cancel` force-removes that one, and a probe
            # sharing it would race the build container it precedes. `--rm` reaps this one.
            name=f"{sandbox_name(ctx.run_id, ctx.repo_id, ctx.attempt)}-cc-probe",
            command=("sh", "-c", C_TOOLCHAIN_PROBE),
            memory=payload.container_memory,
            cpus=payload.container_cpus,
            network=payload.container_network,
            user=current_user_spec(),
        )
        result = await runner(
            tuple(docker_run_argv(spec)),
            deadline=ctx.deadline,
            timeout_s=C_TOOLCHAIN_PROBE_TIMEOUT_S,
        )
        if result.ok:
            return None
        if result.exit_code == _DOCKER_CANNOT_RUN:
            # `docker run` never started a container, so the probe's verdict is not "no compiler"
            # — nothing in the image was consulted at all. Saying otherwise sends an operator to
            # audit an image that may not exist on this host.
            return WorkerError(
                failure_class=FailureClass.BUILD_ERROR,
                retryable=False,
                exit_code=result.exit_code,
                stderr_tail=(
                    f"the sandbox image {payload.image} could not be RUN: `docker run` exited "
                    f"{_DOCKER_CANNOT_RUN}, which means the container never started — so this "
                    "says nothing about whether the image has a C compiler; the probe never "
                    "executed inside it. The usual cause is an image that is not present locally "
                    "and cannot be pulled: `settings.verify.container_image` names a LOCAL tag "
                    "because this fleet has no registry. Build it with `docker build -f "
                    "docker/fleet-build.Dockerfile -t <settings.verify.container_image> docker/`, "
                    "or point that setting at an image this host has. Other things `docker run` "
                    "reports as 125: an unreachable daemon and an invalid flag or resource "
                    "value (e.g. a malformed `container_memory`/`container_cpus`). Re-running an "
                    "identical rung cannot build an image, which is why this is not retryable. "
                    f"docker stderr: {result.stderr_tail or result.stdout_tail}"
                ),
            )
        return WorkerError(
            failure_class=FailureClass.BUILD_ERROR,
            retryable=False,
            exit_code=result.exit_code,
            stderr_tail=(
                f"no C compiler in the sandbox image {payload.image}: "
                f"`{C_TOOLCHAIN_PROBE}` exited {result.exit_code} inside it. "
                "Bazel cannot build a single target without one, and that probe mirrors Bazel's "
                "own lookup (rules_cc `cc/private/toolchain/unix_cc_configure.bzl`): its C++ "
                "autoconfiguration repository `local_config_cc` is generated by resolving `gcc` "
                "on PATH, or the image's `CC` when it sets one — `cc` and `clang` are never "
                "looked for — and when nothing resolves, the FETCH of that repository fails at "
                "loading time with `Cannot find gcc or CC`, before analysis begins. "
                "`@@rules_go+//:stdlib` depends on `local_config_cc//:cc-compiler-k8`, so this is "
                "NOT a cgo-only requirement: a pure-Go package with no `import \"C\"` anywhere "
                "fails too, and under `--keep_going` bazel reports `Found 0 targets` — a "
                "BUILD_ERROR indistinguishable from a broken package. Fix it in the IMAGE: "
                "install a C toolchain providing `gcc`, or give the image an `ENV CC=<name or "
                "absolute path>`. A `CC` in the harness's own environment cannot help — "
                "buildverify passes no env to either container, so `docker run` is emitted with "
                "no `--env` at all. Re-running this rung cannot do either, which is why this is "
                "not retryable. "
                f"Probe stderr: {result.stderr_tail or result.stdout_tail}"
            ),
        )

    # ------------------------------------------------------------------ the work

    async def run(
        self, ctx: WorkerContext, payload: BuildverifyInput
    ) -> WorkerResult[BuildverifyOutput]:
        worktree = Path(payload.worktree or ctx.workdir)
        runner = self._runner_for(ctx, payload)
        output = BuildverifyOutput(
            dest=payload.dest,
            integration_ref=payload.integration_ref,
            baseline_test_count=payload.baseline_test_count,
        )
        units = [BUILD_UNIT] + ([TEST_UNIT] if payload.run_tests else [])
        completed: list[str] = []
        usage = TokenUsage()

        # §11.3, before the first invocation and before any container start: a Bazel run that
        # meets ENOSPC halfway leaves a poisoned action cache AND takes the state DB's write
        # transaction down with it. Refusing costs one `statvfs`; not refusing cost this host
        # every byte it had.
        try:
            require_free_space(
                worktree, payload.min_free_bytes, operation=f"bazel build //{payload.dest}/..."
            )
        except DiskFloorBreached as breach:
            return WorkerResult[BuildverifyOutput](
                status="failed",
                output=output,
                remaining_units=units,
                error=WorkerError(
                    failure_class=FailureClass.DISK_EXHAUSTED,
                    retryable=False,
                    stderr_tail=str(breach),
                    exception_type=f"{type(breach).__module__}.{type(breach).__qualname__}",
                ),
                evidence=[payload.integration_ref],
            )

        # …and before the first `bazel`, for a sandboxed run: an image with no C compiler makes
        # EVERY target unanalysable, Go or not, cgo or not. See `_c_toolchain_gate` for the
        # mechanism and for why re-running cannot fix it.
        c_toolchain = await self._c_toolchain_gate(ctx, payload, runner)
        if c_toolchain is not None:
            return WorkerResult[BuildverifyOutput](
                status="failed",
                output=output,
                remaining_units=units,
                error=c_toolchain,
                evidence=[payload.integration_ref],
            )

        for index, unit in enumerate(units):
            if ctx.cancelled() or ctx.expired(loop_now()):
                # Landed work is real work: a green build followed by a cancelled test run is
                # `partial`, and re-entry owes only the test (§7.1).
                return self._interrupted(ctx, output, completed, units[index:])

            argv = self._argv(ctx, payload, unit=unit, worktree=worktree)
            result = await runner(argv, cwd=worktree, deadline=ctx.deadline)
            output.steps.append(
                StepRecord(
                    unit=unit,
                    command=list(argv),
                    exit_code=result.exit_code,
                    duration_ms=result.duration_ms,
                    timed_out=result.timed_out,
                    ok=result.ok,
                    log_path=None if result.stderr_path is None else str(result.stderr_path),
                    stdout_log_path=(
                        None if result.stdout_path is None else str(result.stdout_path)
                    ),
                )
            )
            if unit == BUILD_UNIT:
                output.build_ok = result.ok
            else:
                output.no_test_targets = no_test_targets(result, unit=unit)
                output.tests_ran = not output.no_test_targets
                output.test_ok = result.ok

            # Exit 4 on a repo that never had tests is not a failure worth recording: the build
            # completed successfully (exit 1 dominates 4, so a broken tree cannot reach here) and
            # there was simply nothing to run. The unit is complete, no attempt is charged, and
            # no diagnosis is prompted for. It IS a failure when the §12.11 baseline says the repo
            # had tests and the migration lost them — `tests_lost` is that one case.
            nothing_to_test = output.no_test_targets and not output.tests_lost
            if not result.ok and not nothing_to_test:
                error = error_from_proc(result, unit=unit)
                usage = await self._diagnose(ctx, payload, result, unit=unit, output=output)
                return WorkerResult[BuildverifyOutput](
                    status="timeout" if result.timed_out else "failed",
                    output=output,
                    completed_units=completed,
                    remaining_units=units[index:],
                    error=error,
                    usage=usage,
                    evidence=[payload.integration_ref, *self._log_refs(output)],
                )
            completed.append(unit)

        return WorkerResult[BuildverifyOutput](
            status="ok",
            output=output,
            completed_units=completed,
            usage=usage,
            evidence=[payload.integration_ref, *self._log_refs(output)],
        )

    async def on_cancel(self, ctx: WorkerContext) -> None:
        """Kill the container by NAME (§11.1): killing the `docker run` client does not stop it.

        The name is derived from the context alone — `fleet-<run_id>-<repo>-<attempt>`, the same
        string `sandbox/worktree.py` gives the worktree — so cancellation needs no payload and no
        handle. A removal that fails because nothing by that name exists is the expected case on
        the un-containerised path and is deliberately not an error.
        """
        sandbox = ContainerSandbox(runner=self._runner or run)
        with contextlib.suppress(OSError, ValueError):
            await sandbox.remove(sandbox_name(ctx.run_id, ctx.repo_id, ctx.attempt))

    # ------------------------------------------------------------------ internals

    def _runner_for(self, ctx: WorkerContext, payload: BuildverifyInput) -> CommandRunner:
        if self._runner is not None:
            return self._runner
        return LoggedRunner(
            Path(payload.log_dir) / str(ctx.run_id), stem=f"{self.name}-{ctx.attempt}"
        )

    def _bazel_argv(self, payload: BuildverifyInput, *, unit: str) -> tuple[str, ...]:
        """`bazel <build|test> //<dest>/...` with the §3.4 flags that make failures machine-read.

        **The cache flags come from the mounts** (`CacheMount.flag`), not from a second copy of
        the configured paths, and they are addressed to whichever side bazel is about to run on:
        `payload.image is not None` is the containerised case, where the only path that exists is
        the bind mount's `/cache/<name>` target; `image is None` is the host run, where the
        container path does not exist at all and the host path is the correct one. A mount that
        is not in `cache_mounts` contributes no flag — never a path that is not there.

        Without these the mounts were inert, which under `--network=none` is not a lost cache hit
        but a build that cannot resolve a single module: nothing to fetch `go_sdk.download` or any
        BCR `bazel_dep` FROM, and no network to fall back to.

        **`extra_args` still wins, and that is checked rather than assumed.** They are appended
        last, and Bazel's option parser keeps the LAST occurrence of a non-`allowMultiple` option:
        `bazel canonicalize-flags --for_command=build -- --disk_cache=/a --disk_cache=/b` prints
        `--disk_cache=/b` on the vendored 9.2.0 (same for `--repository_cache`, same under
        `--for_command=test`). So an operator who hand-wrote either flag into `extra_args` — the
        only way to get one before this method emitted them — keeps the behaviour they had, with
        a duplicate on the command line rather than an error. `.bazelrc` is the other overriding
        layer and is unaffected: command-line flags beat `build:`/`common:` lines either way.
        """
        argv = [payload.bazel_bin, unit, f"//{payload.dest.strip('/')}/..."]
        if payload.keep_going:
            argv.append("--keep_going")
        if payload.build_event_json:
            argv.append(f"--build_event_json_file=bazel-{unit}-events.json")
        if payload.jobs is not None:
            argv.append(f"--jobs={payload.jobs}")
        argv.extend(
            cache.flag(sandboxed=payload.image is not None) for cache in payload.cache_mounts
        )
        argv.extend(payload.extra_args)
        return tuple(argv)

    def _argv(
        self, ctx: WorkerContext, payload: BuildverifyInput, *, unit: str, worktree: Path
    ) -> tuple[str, ...]:
        """The command as executed: bare `bazel` on the host path, or the ADR-0010 container.

        The container is `--network=none`, memory- and CPU-capped and named after the attempt, so
        a build that "passes" by fetching a dependency from the internet is impossible by
        construction and a crashed run leaves a reapable name behind.

        The cache bind mounts are the SAME `CacheMount` objects `_bazel_argv` just turned into
        `--disk_cache=`/`--repository_cache=`, so the directory bazel is told about and the
        directory docker mounts are one derivation — a `--volume` with no flag behind it was the
        whole defect this pairing removes.
        """
        command = self._bazel_argv(payload, unit=unit)
        if payload.image is None:
            return command
        spec = spec_for_attempt(
            run_id=ctx.run_id,
            repo=ctx.repo_id,
            attempt=ctx.attempt,
            image=payload.image,
            command=command,
            worktree=worktree,
            container_workdir=payload.container_workdir,
            memory=payload.container_memory,
            cpus=payload.container_cpus,
            network=payload.container_network,
            extra_mounts=[cache.mount() for cache in payload.cache_mounts],
            min_free_bytes=payload.min_free_bytes,
        )
        return tuple(docker_run_argv(spec))

    def _interrupted(
        self,
        ctx: WorkerContext,
        output: BuildverifyOutput,
        completed: list[str],
        remaining: list[str],
    ) -> WorkerResult[BuildverifyOutput]:
        status: WorkerStatus = "cancelled" if ctx.cancelled() else "timeout"
        if completed:
            return WorkerResult[BuildverifyOutput](
                status="partial",
                output=output,
                completed_units=completed,
                remaining_units=remaining,
                evidence=[output.integration_ref],
            )
        return WorkerResult[BuildverifyOutput](
            status=status,
            output=output,
            remaining_units=remaining,
            error=WorkerError(
                failure_class=FailureClass.TIMEOUT,
                retryable=True,
                stderr_tail=f"{self.name}: stopped before {remaining} on {status}",
            ),
            evidence=[output.integration_ref],
        )

    @staticmethod
    def _log_refs(output: BuildverifyOutput) -> list[str]:
        return [step.log_path for step in output.steps if step.log_path is not None]

    async def _diagnose(
        self,
        ctx: WorkerContext,
        payload: BuildverifyInput,
        result: ProcResult,
        *,
        unit: str,
        output: BuildverifyOutput,
    ) -> TokenUsage:
        """`build_diagnosis` on rungs 2–3 (§3.3 LLM slots), from the VERBATIM stderr.

        Rung 1 is deterministic — `ctx.context_policy is None` — so no prompt is rendered at all,
        and the model is never asked whether the build passed: its answer is stored as advice
        beside a classification the exit code already decided (Rule 5).
        """
        if ctx.context_policy is None:
            return TokenUsage()
        evidence: Evidence = {
            "dest": payload.dest,
            "integration_ref": payload.integration_ref,
            "step": unit,
            "exit_code": result.exit_code,
            "command": list(result.argv),
            "stderr_tail": result.stderr_tail,
            "log_path": None if result.stderr_path is None else str(result.stderr_path),
        }
        try:
            response = await diagnose_build(ctx.llm, evidence, budget=ctx.budget)
        except LlmError:
            # Diagnosis is advice. Losing it must never turn a recorded build failure into an
            # unrecorded worker crash — the failure itself is the thing that has to be persisted.
            return TokenUsage()
        output.diagnosis = response.value.root_cause
        output.diagnosis_failure_class = response.value.failure_class
        return response.usage
