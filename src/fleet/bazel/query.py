"""`bazel query` construction, output parsing, and the rdeps closure sample (SPEC §3.4 step 2).

Two halves, deliberately split so the interesting one needs no `bazel` binary:

* **Pure** — building the query text and the argv, parsing labels out of stdout, and choosing
  the sample. Every property §3.4 asserts about the closure is a property of *this* half, so it
  is tested offline and byte-for-byte.
* **`rdeps_closure`** — the one coroutine that shells out, through an injected `CommandRunner`
  (`util/proc.py`, CLAUDE.md guardrail 3). A test asserts on the argv that *would* have run.

**A capped closure is disclosed, never silent.** §3.4's success criterion asks for the *full*
rdeps closure while its bounds table caps the tested set at a sample; the two readings are one
criterion because the reduction is declared. Under `rdeps_limit` the closure is tested whole and
`rdeps_truncated` is false. Over it, the tested set is **all direct rdeps plus `sample_n` of the
remainder chosen by stable hash of the target label**, `rdeps_truncated` is true, the seed is
recorded so the choice is reproducible and auditable, and `VerificationReport` derives
`Equivalence.CLOSURE_SAMPLED` from that flag — which forces the PR to a draft. A run that
reported 40 000 untested targets as green, or one that stalled forever, are the two failures
this bound exists to sit between.

The hash is `sha256`, not `hash()`: `hash()` of a `str` is salted per process, so the "sample"
would differ between the run that chose it and the resume that had to reproduce it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Final

from fleet.models.enums import Equivalence
from fleet.util.proc import CommandRunner, ProcResult

__all__ = [
    "DEFAULT_RDEPS_LIMIT",
    "DEFAULT_SAMPLE_N",
    "BazelQueryError",
    "RdepsClosure",
    "affected_query",
    "bazel_test_argv",
    "direct_rdeps_query",
    "kind_rule_query",
    "parse_target_labels",
    "query_argv",
    "query_stdout",
    "rdeps_closure",
    "rdeps_query",
    "registry_args",
    "sample_seed_for",
    "select_tested_targets",
    "tests_query",
    "write_target_pattern_file",
]

DEFAULT_RDEPS_LIMIT: Final = 2000
"""`verify.rdeps_limit` (§3.4 bounds table). Above it the closure is sampled, with disclosure."""

DEFAULT_SAMPLE_N: Final = 500
"""`verify.rdeps_sample_n` — the size of the sampled remainder, on top of every direct rdep."""

_BAZEL: Final = "bazel"


class BazelQueryError(RuntimeError):
    """A non-zero `bazel query`. Never swallowed: an empty target list from a failed query is
    indistinguishable from a clean closure of zero, and one of those two is a green PR."""

    def __init__(self, query: str, result: ProcResult, reason: str = "") -> None:
        self.query = query
        self.result = result
        detail = reason or (
            f"exited {result.exit_code} (timed_out={result.timed_out}): "
            f"{result.stderr_tail.strip()[-500:]}"
        )
        super().__init__(f"bazel query {query!r} {detail}")


# =======================================================================================
# query construction
# =======================================================================================


def kind_rule_query(dest: str) -> str:
    """The targets this repo actually owns — `kind(rule, //<dest>/...)` (§3.4 bounds table)."""
    return f"kind(rule, //{dest.strip('/')}/...)"


def rdeps_query(dest: str, *, affected_only: bool = True) -> str:
    """`rdeps(//..., …)` — the blast-radius query, in its two configured forms.

    `verify.affected_only=true` (the default) intersects the universe with the rules this repo's
    commits changed, which is what keeps 250 repos × 4 phases × 3 attempts from becoming 3 000
    full-fleet builds. `false` restores the whole closure for a final gate run.
    """
    if affected_only:
        return f"rdeps(//..., set({kind_rule_query(dest)}))"
    return f"rdeps(//..., //{dest.strip('/')}/...)"


def direct_rdeps_query(dest: str, *, affected_only: bool = True) -> str:
    """Depth-1 rdeps. Every direct rdep is in the tested set even when the closure is sampled —
    a change's immediate consumers are the ones a reviewer assumes were built."""
    return f"{rdeps_query(dest, affected_only=affected_only)[:-1]}, 1)"


def affected_query(dest: str, *, affected_only: bool = True) -> str:
    """Alias kept for call sites that read better as "the affected set"."""
    return rdeps_query(dest, affected_only=affected_only)


def tests_query(dest: str) -> str:
    """Every test target this repo's migrated package declares — `tests(//<dest>/...)`.

    §12.11's test-count comparison sub-clause: `bazel query 'tests(//<dest>/...)' | wc -l` is
    what "the tests survived the move" measures, against `repos.baseline_test_count` — a real
    count, not the boolean "did any test target run at all" `no_test_targets`/`tests_lost` already
    answer. Mirrors `kind_rule_query`'s shape: one line, no state.
    """
    return f"tests(//{dest.strip('/')}/...)"


def registry_args(registry: str | None) -> tuple[str, ...]:
    """`--registry=<url>` when `build.registry` is set, nothing when it is not.

    Nothing — rather than spelling out Bazel's default — because `--registry` REPLACES the
    built-in list rather than adding to it, so emitting the default explicitly would turn a
    future Bazel that ships a second registry into a silently narrowed resolution.
    """
    return () if not registry else (f"--registry={registry}",)


def query_argv(
    query: str,
    *,
    output: str = "label",
    keep_going: bool = True,
    registry: str | None = None,
    cache_flags: Sequence[str] = (),
    extra: Sequence[str] = (),
) -> tuple[str, ...]:
    """argv for one `bazel query`, as a tuple — never a string, so no shell is ever involved.

    `registry` is `build.registry` (§9). It goes on *before* `extra`, so a run that adds a second
    `--registry=` through `extra_args` gets a fallback list in the order it wrote it rather than
    ahead of the configured one.

    `cache_flags` are already-rendered flag strings — `CacheMount.flag(...)` output, addressed to
    the side bazel will run on by the CALLER. They are strings rather than `CacheMount` objects
    on purpose: `CacheMount` lives in `workers/buildverify.py`, and this module is under it, so
    importing the model here would invert the layering and duplicate the one flag renderer. Which
    caches are worth handing to a `query` is likewise the caller's call — see
    `RdepverifyWorker._query_cache_flags`, which measured it against the vendored binary.

    They go on before `extra` for the same reason the registry does, and because repeats collapse
    to the LAST occurrence: an operator's hand-written flag keeps winning.
    """
    argv = [_BAZEL, "query", f"--output={output}"]
    if keep_going:
        argv.append("--keep_going")
    argv.extend(cache_flags)
    argv.extend(registry_args(registry))
    argv.extend(extra)
    argv.append(query)
    return tuple(argv)


def bazel_test_argv(
    *,
    pattern_file: Path,
    build_event_json_file: Path | None = None,
    jobs: int | None = None,
    keep_going: bool = True,
    registry: str | None = None,
    extra: Sequence[str] = (),
) -> tuple[str, ...]:
    """`bazel test` over the verified target set (§3.4 step 2).

    The targets go in a `--target_pattern_file` rather than argv: a 2 000-label command line is
    the argv explosion the bounds table names. `--keep_going` so one broken target cannot hide
    the other nineteen, and `--build_event_json_file` so `FailureClass` comes from the BEP
    instead of a regex over stderr.
    """
    argv = [_BAZEL, "test", f"--target_pattern_file={pattern_file}"]
    if keep_going:
        argv.append("--keep_going")
    if build_event_json_file is not None:
        argv.append(f"--build_event_json_file={build_event_json_file}")
    if jobs is not None:
        argv.append(f"--jobs={jobs}")
    argv.extend(registry_args(registry))
    argv.extend(extra)
    return tuple(argv)


def write_target_pattern_file(targets: Sequence[str], path: Path) -> Path:
    """One label per line, in the tested order. Returns the path for `bazel_test_argv`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{t}\n" for t in targets), encoding="utf-8")
    return path


# =======================================================================================
# output parsing and sampling
# =======================================================================================


def parse_target_labels(stdout: str) -> tuple[str, ...]:
    """Labels out of `--output=label`, deduplicated and **sorted**.

    Sorted because `bazel query` makes no ordering promise and the order reaches a persisted
    decision (which targets were sampled, §11.6); an unsorted parse makes the seed reproducible
    and the sample still not.
    """
    labels = {
        line.strip()
        for line in stdout.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "WARNING:", "INFO:", "Loading:"))
    }
    return tuple(sorted(labels))


def sample_seed_for(query: str) -> str:
    """A deterministic default seed: the query decides it, so a resume re-derives it for free."""
    return sha256(query.encode("utf-8")).hexdigest()[:16]


def _rank(seed: str, label: str) -> str:
    return sha256(f"{seed}\x00{label}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class RdepsClosure:
    """The blast-radius result, with its own honesty built in (§3.4).

    `tested` is what `bazel test` will actually run; `total_count` is what the closure really
    was. When those differ, `truncated` is true and `equivalence` is `CLOSURE_SAMPLED` — the
    three fields travel together so a caller cannot record the reduced set and the unqualified
    verdict at the same time.
    """

    query: str
    total_count: int
    tested: tuple[str, ...]
    direct: tuple[str, ...] = ()
    truncated: bool = False
    seed: str = ""
    sample_n: int = 0

    @property
    def equivalence(self) -> Equivalence:
        """`CLOSURE_SAMPLED` whenever the closure was capped — never `FULL` (§3.5.1)."""
        return Equivalence.CLOSURE_SAMPLED if self.truncated else Equivalence.FULL

    @property
    def tested_count(self) -> int:
        return len(self.tested)


def select_tested_targets(
    closure: Sequence[str],
    *,
    query: str,
    direct: Sequence[str] = (),
    limit: int = DEFAULT_RDEPS_LIMIT,
    sample_n: int = DEFAULT_SAMPLE_N,
    seed: str | None = None,
) -> RdepsClosure:
    """The full closure under the bound; a seeded, reproducible sample above it.

    The sample is *all* direct rdeps plus the `sample_n` remainder labels with the lowest
    `sha256(seed || label)` — a stable hash, so the same seed reproduces the same choice in
    another process, on another machine, on a resume days later.
    """
    all_targets = tuple(sorted(set(closure)))
    total = len(all_targets)
    chosen_seed = seed if seed is not None else sample_seed_for(query)
    if total <= limit:
        return RdepsClosure(
            query=query,
            total_count=total,
            tested=all_targets,
            direct=tuple(sorted(set(direct) & set(all_targets))),
            truncated=False,
            seed=chosen_seed,
            sample_n=0,
        )
    kept = set(direct) & set(all_targets)
    remainder = sorted(
        set(all_targets) - kept, key=lambda label: (_rank(chosen_seed, label), label)
    )
    sampled = set(remainder[:sample_n])
    tested = tuple(sorted(kept | sampled))
    return RdepsClosure(
        query=query,
        total_count=total,
        tested=tested,
        direct=tuple(sorted(kept)),
        truncated=True,
        seed=chosen_seed,
        sample_n=len(sampled),
    )


# =======================================================================================
# the one coroutine that shells out
# =======================================================================================


def query_stdout(query: str, result: ProcResult) -> str:
    """The FULL stdout of a query, or a loud failure.

    `ProcResult.stdout_tail` is capped at 32 KiB at capture time (§11.3) and a 40 000-target
    closure is megabytes: reading the tail would silently drop most of the blast radius and
    report the remainder as the whole of it. So the full stream is read from
    `ProcResult.stdout_path` — which means the injected runner must be one that keeps its logs
    (`functools.partial(run, log_dir=...)`) — and a truncated tail with no such file raises.
    """
    if result.stdout_path is not None:
        return result.stdout_path.read_text(encoding="utf-8", errors="replace")
    if result.stdout_truncated:
        raise BazelQueryError(
            query,
            result,
            reason=(
                f"produced {result.stdout_bytes} bytes but the runner kept no stdout file; the "
                "tail would silently drop most of the closure"
            ),
        )
    return result.stdout_tail


async def _query(
    runner: CommandRunner,
    query: str,
    *,
    cwd: Path | None,
    deadline: float | None,
    registry: str | None = None,
    cache_flags: Sequence[str] = (),
) -> tuple[str, ...]:
    result = await runner(
        query_argv(query, registry=registry, cache_flags=cache_flags), cwd=cwd, deadline=deadline
    )
    if not result.ok:
        raise BazelQueryError(query, result)
    return parse_target_labels(query_stdout(query, result))


async def rdeps_closure(
    dest: str,
    *,
    runner: CommandRunner,
    cwd: Path | None = None,
    deadline: float | None = None,
    affected_only: bool = True,
    limit: int = DEFAULT_RDEPS_LIMIT,
    sample_n: int = DEFAULT_SAMPLE_N,
    seed: str | None = None,
    registry: str | None = None,
    cache_flags: Sequence[str] = (),
) -> RdepsClosure:
    """§3.4 step 2: the closure, and the sample when it exceeds the bound.

    The depth-1 query runs **only** when the closure actually needs sampling: on the ordinary
    path it would be a second `bazel query` per repo per attempt bought for nothing.

    `registry` is `build.registry`, carried onto both queries: a closure resolved against a
    different registry than the one `bazel test` will use is a blast radius measured on a
    different module graph.

    `cache_flags` rides on both queries for the same reason and with the same shape: the depth-1
    query loads the very module graph the first one just fetched, so a cache named on one and not
    the other pays the fetch twice on exactly the repos that are already over the bound.
    """
    query = rdeps_query(dest, affected_only=affected_only)
    targets = await _query(
        runner, query, cwd=cwd, deadline=deadline, registry=registry, cache_flags=cache_flags
    )
    direct: tuple[str, ...] = ()
    if len(targets) > limit:
        direct = await _query(
            runner, direct_rdeps_query(dest, affected_only=affected_only), cwd=cwd,
            deadline=deadline, registry=registry, cache_flags=cache_flags,
        )
    return select_tested_targets(
        targets, query=query, direct=direct, limit=limit, sample_n=sample_n, seed=seed
    )
