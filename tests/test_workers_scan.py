"""The four Phase-1 scan workers, against real git repos and a fake `ModelClient` (SPEC §3.1).

Every test here is about a failure with a cost attached, not about a shape:

* a worker that inherits `preconditions_hold` is **un-instantiable**, so "it has a real one" is
  proven by constructing it — the old defaulted `return True` admitted every re-entry blindly;
* `preconditions_hold` is asked against the FILESYSTEM, because the state it is claiming about
  (a mirror, a worktree, the manifests a checkpoint says it parsed) lives there and a row that
  disagrees with the disk is how a resumed run indexes a tree nobody has read;
* re-entry must not repeat paid work: a second `git clone` of a 5 GB mirror, or a second pass
  over 900 files, is the whole reason the checkpoint exists;
* `symbolindex` interrupted mid-tree reports `partial` and resumes at `remaining_units` — the
  headline behaviour the five-valued `WorkerResult` exists for. Reporting `failed` there would
  re-insert every row it already landed;
* a `github_pat_…` in a clone URL must reach `git` and NOTHING else: not a log line, not a
  `WorkerError`, not the persisted output. §12.20 greps the whole run for that literal.

Coroutines are driven with `asyncio.run`, matching `tests/conftest.py`: the suite stays runnable
without the asyncio plugin. Git is real (a temp repo per test); the network never is.
"""

from __future__ import annotations

import asyncio
import inspect
import shutil
import subprocess
from concurrent.futures import Executor, Future
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import UUID

import pytest

from fleet.llm.client import (
    CallBudget,
    Message,
    ModelResponse,
    SchemaUnsatisfied,
    TierUnavailable,
)
from fleet.llm.roles import SPEC_ROLE_TIERS, LlmRouter
from fleet.llm.schemas import RepoClassification
from fleet.models.enums import (
    Ecosystem,
    FailureClass,
    ModelTier,
    Phase,
    RepoStatus,
    StructuredOutputMode,
)
from fleet.models.tasks import BackendTarget, TokenUsage
from fleet.orchestrator.retry import LadderState, RetryPolicy
from fleet.workers.base import WorkerContext, assert_stateless, implements_preconditions
from fleet.workers.classify import UNIT, ClassifyInput, ClassifyWorker
from fleet.workers.clone import CloneInput, CloneWorker, credential_free
from fleet.workers.interrogate import InterrogateInput, InterrogateWorker, worktree_presence
from fleet.workers.symbolindex import SymbolIndexInput, SymbolindexWorker

RUN_ID = UUID("00000000-0000-4000-8000-0000000005ca")
OWNER = "host:container:99:boot"
#: A SYNTHETIC credential in the exact shape §11.4's `github_pat` detector matches. Not a
#: secret; the point is that the literal must not survive anywhere but the argv handed to git.
TOKEN = "github_pat_11ABCDEFG0123456789abcdefghijklmnopqrstuvwxyz"  # noqa: S105


# =======================================================================================
# fixtures — a real git repo, and the inert half of a WorkerContext
# =======================================================================================


def git(*args: str, cwd: Path) -> str:
    """Fixture-building git. The workers under test use `util.proc`; this is only the setup."""
    done = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "PATH": "/usr/bin:/bin",
            "HOME": str(cwd),
        },
    )
    return done.stdout


SOURCES: dict[str, str] = {
    "pyproject.toml": (
        '[project]\nname = "acme-billing"\nversion = "1.0.0"\n'
        'dependencies = ["acme-commons>=1.2", "requests"]\n'
    ),
    "src/acme/service.py": (
        "import acme.commons\n"
        "from acme.ledger import post\n"
        "\n"
        "class BillingService:\n"
        "    pass\n"
        "\n"
        "def charge(amount):\n"
        "    return post(amount)\n"
    ),
    "src/acme/_private.py": "def _hidden():\n    return 1\n",
    "api/billing.proto": (
        'syntax = "proto3";\npackage acme.billing.v1;\n'
        "service Billing {\n}\nmessage Invoice {\n}\n"
    ),
    "openapi.yaml": "openapi: 3.0.0\ninfo:\n  title: Billing\n",
    "README.md": "# billing\n",
}


def make_source_repo(root: Path, files: dict[str, str] | None = None) -> Path:
    """A real, committed git repo — the thing `git clone --mirror` is pointed at."""
    root.mkdir(parents=True, exist_ok=True)
    git("init", "--initial-branch=main", ".", cwd=root)
    for rel, text in (files if files is not None else SOURCES).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    git("add", "--all", cwd=root)
    git("commit", "-m", "initial", cwd=root)
    return root


@dataclass
class RecordingLog:
    """`ctx.log`, narrowed to "what did the worker say". §12.20 greps exactly this."""

    lines: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def info(self, event: str, **kwargs: Any) -> None:
        self.lines.append((event, kwargs))

    warning = info
    error = info
    debug = info

    def rendered(self) -> str:
        return "\n".join(f"{event} {kwargs}" for event, kwargs in self.lines)


class SyncPool(Executor):
    """A synchronous `Executor` that records which paths it parsed, and can bend the clock.

    Synchronous because the assertion is about WHICH files were handed to the pool, and a real
    `ProcessPoolExecutor` would only add flakiness to that question. `expire_after` mutates the
    context's deadline once N files are done, which is the only deterministic way to express
    "the wall clock ran out mid-tree" — sleeping for a real deadline is a race.
    """

    def __init__(self, ctx: WorkerContext | None = None, expire_after: int | None = None) -> None:
        self.parsed: list[str] = []
        self.ctx = ctx
        self.expire_after = expire_after

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Future[Any]:  # type: ignore[override]
        future: Future[Any] = Future()
        if len(args) > 1:
            self.parsed.append(str(args[1]))
        future.set_result(fn(*args, **kwargs))
        if (
            self.expire_after is not None
            and self.ctx is not None
            and len(self.parsed) >= self.expire_after
        ):
            self.ctx.deadline = asyncio.get_event_loop().time() - 1.0
        return future


@dataclass
class FakeLimits:
    """`Limits` narrowed to the three ceilings a scan worker actually takes out."""

    git_net: asyncio.Semaphore
    subprocess: asyncio.Semaphore
    cpu_pool: Executor
    llm: dict[ModelTier, asyncio.Semaphore]

    def for_tier(self, tier: ModelTier) -> asyncio.Semaphore:
        return self.llm[tier]


class UnavailableModelClient:
    """A `ModelClient` every one of whose targets is unhealthy (§11.8).

    The context ALWAYS carries a client now, so "classify could not reach a model" no longer has
    a mis-wiring spelling. This is what the real remaining version of it looks like, and it is
    what the scan workers that never call a model are handed by default.
    """

    async def complete(
        self, role: str, messages: Any, response_model: Any, **kwargs: Any
    ) -> ModelResponse[Any]:
        raise TierUnavailable(ModelTier.CHEAP, ("fake:fake-cheap",))

    def stream(self, role: str, messages: Any, response_model: Any, **kwargs: Any) -> Any:
        raise NotImplementedError  # pragma: no cover - classify only ever calls complete()

    async def capabilities(self, role: str) -> Any:
        raise NotImplementedError  # pragma: no cover


def make_router() -> LlmRouter:
    """A real router over the shipped role table: `classify` must resolve its tier, not guess."""
    target = BackendTarget(backend="fake", model_id="fake-cheap", price="free")
    return LlmRouter(
        dict(SPEC_ROLE_TIERS), dict.fromkeys(ModelTier, (target,)), profile="test"
    )


def make_ctx(
    workdir: Path,
    *,
    pool: Executor | None = None,
    seconds_left: float = 120.0,
    log: RecordingLog | None = None,
    llm: Any = None,
) -> WorkerContext:
    """`llm` is the ONE call surface (§7.1) and defaults to a client whose tier is unreachable,
    so a worker that calls a model it was not given one for fails the way §11.8 says, not with an
    `AttributeError` on a sentinel."""
    now = monotonic()
    return WorkerContext(
        run_id=RUN_ID,
        repo_id="acme-billing",
        attempt=1,
        workdir=str(workdir),
        lease_owner=OWNER,
        lease_fence=1,
        deadline=now + seconds_left,
        cancel=asyncio.Event(),
        budget=CallBudget(
            remaining_tokens=100_000, remaining_usd=5.0, deadline=now + seconds_left
        ),
        db=object(),  # type: ignore[arg-type]
        llm=UnavailableModelClient() if llm is None else llm,
        router=make_router(),
        limits=FakeLimits(  # type: ignore[arg-type]
            git_net=asyncio.Semaphore(2),
            subprocess=asyncio.Semaphore(4),
            cpu_pool=pool or SyncPool(),
            llm={tier: asyncio.Semaphore(1) for tier in ModelTier},
        ),
        log=log or RecordingLog(),  # type: ignore[arg-type]
    )


class RecordingRunner:
    """A `CommandRunner` that delegates to the real one and remembers every argv.

    Real git, real exit codes — the recording is only so a test can assert that a second scan
    did NOT issue a second `clone`, which no amount of inspecting the resulting directory can
    distinguish from a re-clone that happened to produce the same bytes.
    """

    def __init__(self) -> None:
        self.argvs: list[tuple[str, ...]] = []

    async def __call__(self, argv: Any, **kwargs: Any) -> Any:
        from fleet.util.proc import run as real_run

        self.argvs.append(tuple(argv))
        return await real_run(argv, **kwargs)

    def subcommands(self) -> list[str]:
        """The git verb of each recorded call: `git -C <path> remote update …` → `remote update`.

        Parsed rather than substring-matched, because `clone` also appears inside the mirror path
        of every later call and a naive `in` test would report a re-clone that never happened.
        """
        verbs: list[str] = []
        for argv in self.argvs:
            parts = list(argv)
            if not parts or parts[0] != "git":
                continue
            i = 1
            while i < len(parts):
                if parts[i] in ("-C", "-c"):
                    i += 2
                    continue
                if parts[i].startswith("-"):
                    i += 1
                    continue
                break
            if i >= len(parts):
                continue
            verb = parts[i]
            if verb in ("remote", "worktree") and i + 1 < len(parts):
                verb = f"{verb} {parts[i + 1]}"
            verbs.append(verb)
        return verbs


def clone_once(
    tmp_path: Path, *, runner: RecordingRunner | None = None, **overrides: Any
) -> tuple[Any, CloneInput, WorkerContext, RecordingRunner]:
    """Clone a fresh source repo into a fresh cache + worktree. Returns everything to assert on."""
    source = make_source_repo(tmp_path / "src-repo")
    worktree = tmp_path / "work" / "acme-billing"
    log = RecordingLog()
    ctx = make_ctx(worktree, log=log)
    payload = CloneInput(
        repo_id="acme-billing",
        url=overrides.pop("url", f"file://{source}"),
        cache_dir=str(tmp_path / "cache"),
        **overrides,
    )
    recorder = runner or RecordingRunner()
    worker = CloneWorker(runner=recorder)  # type: ignore[arg-type]
    result = asyncio.run(worker.run(ctx, payload))
    return result, payload, ctx, recorder


# =======================================================================================
# (1) the four workers exist, are concrete, and claim their phase
# =======================================================================================


def test_every_scan_worker_is_instantiable_and_names_phase_scan() -> None:
    """Instantiation IS the proof that `preconditions_hold` is overridden.

    Why it matters: `BaseWorker.preconditions_hold` is abstract precisely so a worker cannot
    inherit "yes, re-enter" by accident. A stub that never overrides it is un-instantiable, so
    `CloneWorker()` succeeding is a mechanical claim, not a comment.
    """
    workers = [CloneWorker(), InterrogateWorker(), ClassifyWorker(), SymbolindexWorker()]
    assert [w.name for w in workers] == ["clone", "interrogate", "classify", "symbolindex"]
    assert {w.phase for w in workers} == {Phase.SCAN}
    for worker in workers:
        assert implements_preconditions(type(worker)), f"{worker.name} inherits re-entry"
        assert_stateless(worker)  # §7.2: registry values are shared singletons


# =======================================================================================
# (2) clone — preconditions, no second clone, preflight gates, credentials
# =======================================================================================


def test_clone_preconditions_are_false_before_the_clone_and_true_after(tmp_path: Path) -> None:
    """`preconditions_hold` reads the disk, so it must flip exactly when the disk does.

    Why it matters: this is the predicate a resume consults before re-entering. If it answered
    True on a fresh repo, Phase 1 would skip a clone that never happened; if it answered False
    after a good clone, every resume would re-fetch 250 mirrors.
    """
    source = make_source_repo(tmp_path / "src-repo")
    worktree = tmp_path / "work" / "acme-billing"
    ctx = make_ctx(worktree)
    payload = CloneInput(
        repo_id="acme-billing", url=f"file://{source}", cache_dir=str(tmp_path / "cache")
    )
    worker = CloneWorker()

    assert asyncio.run(worker.preconditions_hold(ctx, payload)) is False

    result = asyncio.run(worker.run(ctx, payload))
    assert result.status == "ok", result.error
    assert result.output is not None
    assert result.output.head_sha is not None
    assert result.output.default_branch == "main"
    assert result.output.default_branch_source == "symbolic-ref"
    assert result.output.commit_count == 1
    assert result.output.preflight_ok is True
    assert result.output.is_shallow is False
    assert result.output.submodule_count == 0
    # The oversize-blob probe streams `cat-file --batch-check` off disk rather than reading a
    # 32 KiB tail; a zero here would mean it measured nothing and every gate below it is blind.
    assert result.output.largest_blob_bytes > 0
    assert result.output.size_bytes > 0
    assert (worktree / "pyproject.toml").is_file()

    assert asyncio.run(worker.preconditions_hold(ctx, payload)) is True


def test_a_shallow_file_that_declares_no_boundary_is_not_a_shallow_repository(
    tmp_path: Path,
) -> None:
    """`.git/shallow` holding no grafted commit id is not a shallow repository.

    Why it matters: the shallow verdict is the one that survives a *successful* `--unshallow` as a
    non-retryable `PREFLIGHT` gate — straight to `REQUIRES_HUMAN_INTERVENTION`, no attempt charged
    and no attempt possible. An `exists()` check hands that verdict to any complete mirror that
    happens to carry an empty or whitespace-only `shallow` file, and the file is real enough to be
    left behind: it is a plain list of ids, and a truncated write or an interrupted fetch leaves
    one holding nothing. `git rev-parse --is-shallow-repository` has the same blind spot — a
    planted zero-byte `shallow` makes it answer `true` on a complete repo — so reading the file is
    not a shortcut around the predicate; it is strictly better than it, for one `read_text`.

    The second half is the non-vacuity: a file that DOES name a root still reads as shallow, so
    nothing about the gate was widened away.
    """
    from fleet.workers.clone import _is_shallow

    result, payload, ctx, _ = clone_once(tmp_path)
    assert result.status == "ok" and result.output is not None
    mirror = Path(result.evidence[0])
    head = result.output.head_sha
    assert head is not None

    for empty in ("", "\n", "   \n\n\t\n"):
        (mirror / "shallow").write_text(empty, encoding="utf-8")
        assert _is_shallow(mirror) is False, "a `shallow` file naming no root was read as a graft"

    # The zero-byte file end to end, because it is the one git itself accepts and mis-reports:
    # `remote update` succeeds against it while `rev-parse --is-shallow-repository` answers
    # `true`. (A file holding blank or whitespace lines is instead rejected by git with `fatal:
    # bad shallow line`, which is a loud failure of its own and never reaches this verdict.) So
    # this is exactly the state where the old `exists()` check — and the predicate that would
    # have replaced it — sent a complete mirror to a human, and where reading the file does not.
    (mirror / "shallow").write_text("", encoding="utf-8")
    assert subprocess.run(  # noqa: S603
        ["git", "-C", str(mirror), "rev-parse", "--is-shallow-repository"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip() == "true", "git's own predicate stopped having the blind spot this documents"
    rescan = asyncio.run(CloneWorker().run(ctx, payload))
    assert rescan.status == "ok", rescan.error
    assert rescan.output is not None and rescan.output.is_shallow is False

    (mirror / "shallow").write_text(f"{head}\n", encoding="utf-8")
    assert _is_shallow(mirror) is True, "a real shallow boundary stopped being detected"

    (mirror / "shallow").unlink()
    assert _is_shallow(mirror) is False


def test_clone_reentry_refreshes_the_mirror_and_never_clones_twice(tmp_path: Path) -> None:
    """A completed clone re-run is a `remote update`, not a second `git clone`.

    Why it matters: a re-scan of a 250-repo fleet that re-clones is hours of network and disk
    for a tree that is already on disk — and §3.1 step 1 states the incremental contract
    explicitly ("existing mirrors are `git remote update`d").
    """
    recorder = RecordingRunner()
    result, payload, ctx, _ = clone_once(tmp_path, runner=recorder)
    assert result.status == "ok"
    assert recorder.subcommands().count("clone") == 1

    second = asyncio.run(CloneWorker(runner=recorder).run(ctx, payload))  # type: ignore[arg-type]

    assert second.status == "ok"
    assert recorder.subcommands().count("clone") == 1, "the mirror was cloned a second time"
    assert recorder.subcommands().count("remote update") == 1, "the mirror was not refreshed"
    assert second.output is not None
    assert second.output.head_sha == result.output.head_sha


def test_clone_checkpoint_skips_the_unit_that_already_landed(tmp_path: Path) -> None:
    """A `partial` checkpoint's owed units are the ONLY ones re-entry runs.

    Why it matters: `remaining_units` is what the runner hands back after an interrupted attempt.
    A worker that ignored it would re-fetch the mirror it just fetched — the blind replay the
    five-valued result exists to make impossible.
    """
    recorder = RecordingRunner()
    result, payload, ctx, _ = clone_once(tmp_path, runner=recorder)
    assert result.status == "ok"
    before = list(recorder.subcommands())

    resumed = payload.model_copy(update={"remaining_units": ()})
    again = asyncio.run(CloneWorker(runner=recorder).run(ctx, resumed))  # type: ignore[arg-type]

    after = recorder.subcommands()[len(before) :]
    assert again.status == "ok"
    assert "remote update" not in after, "the mirror unit re-ran although it was not owed"
    assert "clone" not in after
    assert set(again.completed_units) == {"mirror", "worktree"}


def test_clone_gates_an_oversize_repo_with_a_structured_non_retryable_error(
    tmp_path: Path,
) -> None:
    """A repo the fleet refuses to migrate fails LOUD and typed, never as a bool or prose.

    Why it matters: `retry.py` branches on `retryable`, and `execute()` turns a non-retryable
    failure into `REQUIRES_HUMAN_INTERVENTION` for that repo while the fleet continues. A gate
    reported as a generic failure would burn all three ladder rungs re-measuring the same size.
    """
    result, _, _, _ = clone_once(tmp_path, max_repo_bytes=1)

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.failure_class is FailureClass.PREFLIGHT
    assert result.error.retryable is False
    assert "max_repo_bytes" in result.error.stderr_tail


def test_clone_records_an_empty_repo_as_a_finding_not_a_failure(tmp_path: Path) -> None:
    """§3.1 step 1: an empty repo is `SKIPPED`, "not an error" — and gets no worktree.

    Why it matters: a fleet manifest listing a repo that was never pushed to must not stop the
    run, and cutting a worktree from a commit that does not exist would fail with a git error
    that looks like infrastructure trouble.
    """
    empty = tmp_path / "empty-repo"
    empty.mkdir()
    git("init", "--initial-branch=main", ".", cwd=empty)
    worktree = tmp_path / "work" / "empty"
    ctx = make_ctx(worktree)
    payload = CloneInput(
        repo_id="empty-repo", url=f"file://{empty}", cache_dir=str(tmp_path / "cache")
    )

    result = asyncio.run(CloneWorker().run(ctx, payload))

    assert result.status == "ok"
    assert result.output is not None
    assert result.output.findings == ("EmptyRepo",)
    assert result.output.preflight_ok is False
    assert result.output.worktree_path is None
    assert not worktree.exists()


def test_clone_interrupted_with_nothing_landed_discriminates_cancelled_from_timeout() -> None:
    """`_interrupted()` (D138): a genuine `ctx.cancelled()` is an operator decision and stays
    `cancelled` (not an attempt) -- `TRANSIENT_INFRA`; a genuine `ctx.expired()` is a real
    timeout and must be a chargeable `timeout` result (§11) with `failure_class=TIMEOUT` --
    conflating the two would let a repo that always times out cutting its worktree never
    escalate to REQUIRES_HUMAN_INTERVENTION.

    Called directly on `_interrupted()` (the codebase's own precedent for unit-testing a
    worker's private helper, e.g. `worker._error_for` above) rather than driven through `run()`:
    `UNITS = ("mirror", "worktree")` and the mirror unit is always appended to `completed`
    before either of `run()`'s two call sites can be reached, so the `not done` (nothing landed)
    branch this test targets is unreachable with an empty `completed` via `run()` itself --
    `_interrupted()` is the unit the discrimination actually lives in.
    """
    worker = CloneWorker()

    cancelled = worker._interrupted([], ["mirror", "worktree"], timed_out=False)
    assert cancelled.status == "cancelled"
    assert cancelled.error is not None
    assert cancelled.error.failure_class is FailureClass.TRANSIENT_INFRA

    timed_out = worker._interrupted([], ["mirror", "worktree"], timed_out=True)
    assert timed_out.status == "timeout"
    assert timed_out.error is not None
    assert timed_out.error.failure_class is FailureClass.TIMEOUT


# ---------------------------------------------------------------------------------------
# (2b) clone — a probe that produced no answer must produce no verdict
#
# `util.proc.run` synthesises a deadline that passed before the call as `timed_out=True` AND
# `started=False` AND `exit_code=124`, together; `ProcResult.ok` is
# `started and not timed_out and exit_code == 0`. So `if not result.ok` collapses "never ran",
# "killed at the deadline", "ran and exited non-zero" and "ran and the answer was legitimately
# no". Each test below forces exactly one probe into the first two states with REAL git doing
# everything else, and asserts the WRONG verdict is absent — the misattribution is the defect,
# so proving the right one appears somewhere is not enough.
# ---------------------------------------------------------------------------------------


class StalledRunner:
    """Real git for every call, except the one whose argv matches `stall` — which comes back the
    way `util.proc.run` reports a call it never made, or a process it killed at the deadline.

    Delegating the rest is the point: the mirror, the branch probe and the commit count are all
    genuine, so the worker reaches the probe under test in the state a real run reaches it in.
    """

    def __init__(self, stall: str, *, started: bool = False, exit_code: int = 124) -> None:
        self.stall = stall
        self.started = started        # False = the deadline had already passed; nothing spawned
        self.exit_code = exit_code    # 124 for never-started, a negative signal for a kill
        self.stalled: list[tuple[str, ...]] = []

    async def __call__(self, argv: Any, **kwargs: Any) -> Any:
        from fleet.util.proc import ProcResult
        from fleet.util.proc import run as real_run

        parts = tuple(argv)
        if any(self.stall in part for part in parts):
            self.stalled.append(parts)
            return ProcResult(
                argv=parts,
                exit_code=self.exit_code,
                stdout_tail="",
                stderr_tail="",
                duration_ms=0,
                timed_out=True,
                started=self.started,
                cwd=kwargs.get("cwd"),
            )
        return await real_run(argv, **kwargs)


def test_an_unshallow_that_never_ran_is_not_a_permanent_preflight_verdict(
    tmp_path: Path,
) -> None:
    """A `git fetch --unshallow` that produced no answer is retryable — a *settled* refusal stays
    a non-retryable `PREFLIGHT` gate.

    Why it matters: `PREFLIGHT` means "the repo's own shape; identical on every attempt", and
    `RetryPolicy` sends it straight to `REQUIRES_HUMAN_INTERVENTION` without charging an attempt.
    The unshallow is the most transient thing this worker does — it holds `ctx.limits.git_net` and
    talks to a remote — so a TCP reset or a passed deadline classified there makes a blip an
    immutable property of the repository that only a human can clear. The second half of this test
    is the other half of the bargain: nothing about retryability was widened for a mirror that
    genuinely cannot be unshallowed.
    """
    result, payload, ctx, _ = clone_once(tmp_path)
    assert result.status == "ok" and result.output is not None
    mirror = Path(result.evidence[0])
    # The mirror the worker just built is complete; a `shallow` file is exactly and only what
    # `_preflight` reads to decide it is not.
    (mirror / "shallow").write_text(f"{result.output.head_sha}\n", encoding="utf-8")

    stalled = StalledRunner("--unshallow")
    blipped = asyncio.run(CloneWorker(runner=stalled).run(ctx, payload))  # type: ignore[arg-type]

    assert stalled.stalled, "the fetch was never reached; the test proves nothing"
    assert blipped.status == "failed"
    assert blipped.error is not None
    assert blipped.error.failure_class is not FailureClass.PREFLIGHT, (
        "a fetch that never ran was reported as the repository's own immutable shape"
    )
    assert blipped.error.failure_class is not FailureClass.TIMEOUT, (
        "a fetch that was never SPAWNED was reported as one that ran too long — `TIMEOUT` is "
        "substantive on the ladder, so that misattribution charges a rung for a measurement "
        "nobody took"
    )
    assert blipped.error.retryable is True
    assert blipped.error.failure_class is FailureClass.TRANSIENT_INFRA
    # …and the verdict the classification feeds: no human is summoned for a network blip, and —
    # the point of the class, not a corollary of it — no rung is charged for it either.
    decision = RetryPolicy().decide(LadderState(), blipped.error)
    assert decision.terminal_status is not RepoStatus.REQUIRES_HUMAN_INTERVENTION
    assert decision.charges_attempt is False
    assert decision.state.attempts == 0

    # The settled answer is unchanged: the operator disabled unshallowing, so the mirror's shape
    # IS the verdict and it is still non-retryable PREFLIGHT.
    settled = asyncio.run(
        CloneWorker().run(ctx, payload.model_copy(update={"unshallow": False}))
    )
    assert settled.status == "failed"
    assert settled.error is not None
    assert settled.error.failure_class is FailureClass.PREFLIGHT
    assert settled.error.retryable is False
    assert RetryPolicy().decide(LadderState(), settled.error).terminal_status is (
        RepoStatus.REQUIRES_HUMAN_INTERVENTION
    )


def test_a_rev_parse_that_never_ran_is_not_an_empty_repo(tmp_path: Path) -> None:
    """A head probe with no answer must not be reported as an empty repository — and must not be
    reported as `ok` at all.

    Why it matters: `Git.resolve` is `rev-parse --verify --quiet` with `check=False` returning
    `sha if result.ok and sha else None`, so a passed deadline is indistinguishable from "the rev
    does not exist". Read as `EmptyRepo`, the worker returns SUCCESS with `head_sha=None`, cuts no
    worktree, and persists a durable finding about a repo that has commits — after which every
    later worker reports "worktree does not exist; run the clone worker first". A wrong finding
    delivered as success is worse than a wrong failure: nothing downstream ever re-asks.
    """
    result, payload, ctx, _ = clone_once(tmp_path)
    assert result.status == "ok" and result.output is not None
    assert result.output.findings == ()  # this repo has a commit; it is not empty

    stalled = StalledRunner("rev-parse")
    blind = asyncio.run(CloneWorker(runner=stalled).run(ctx, payload))  # type: ignore[arg-type]

    assert stalled.stalled, "the head probe was never reached; the test proves nothing"
    assert blind.status != "ok", "a repo whose head was never resolved was reported as scanned"
    assert "EmptyRepo" not in blind.model_dump_json(), (
        "a rev-parse that never ran was published as a durable EmptyRepo finding"
    )
    assert blind.output is None, "preflight columns were published from a probe that never ran"
    assert blind.error is not None
    assert blind.error.retryable is True
    assert blind.error.failure_class is not FailureClass.TIMEOUT, (
        "a rev-parse that was never spawned was reported as one killed at its deadline"
    )
    assert blind.error.failure_class is FailureClass.TRANSIENT_INFRA
    assert RetryPolicy().decide(LadderState(), blind.error).charges_attempt is False


def test_an_unmeasured_preflight_probe_is_never_published_as_a_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`submodule_count`, `has_lfs` and `largest_blob_bytes` are columns other phases read as
    facts. A probe that never ran must not fill one in.

    Why it matters, per probe: `has_lfs=False` DISARMS the `git-lfs`-on-PATH gate two lines later,
    so a killed `git show :.gitattributes` waves through the exact repo the gate exists to stop;
    `largest_blob_bytes=0` is a *measured* number, and `0` for a `cat-file` that never ran means
    `OversizeBlob` can never fire; `submodule_count=0` silently drops `SubmodulePresent`. All
    three were invisible because the worker still returned `ok` — the finding was wrong and the
    status said it was fine.
    """
    result, payload, ctx, _ = clone_once(tmp_path)
    assert result.status == "ok" and result.output is not None
    assert (result.output.submodule_count, result.output.has_lfs) == (0, False)
    assert result.output.largest_blob_bytes > 0  # the real measurement, for contrast

    # (a) the submodule probe, killed at its deadline (started, then SIGKILLed)
    submodules = StalledRunner(":.gitmodules", started=True, exit_code=-9)
    unmeasured = asyncio.run(CloneWorker(runner=submodules).run(ctx, payload))  # type: ignore[arg-type]
    assert submodules.stalled and unmeasured.status != "ok"
    assert unmeasured.output is None, "submodule_count=0 was published for a probe that was killed"
    assert unmeasured.error is not None and unmeasured.error.retryable is True
    # This one DID run and was killed at the deadline, so it is substantive `TIMEOUT` and charging
    # a rung for it is correct — the other half of the bargain the never-started cases strike.
    assert unmeasured.error.failure_class is FailureClass.TIMEOUT
    assert RetryPolicy().decide(LadderState(), unmeasured.error).charges_attempt is True

    # (b) the LFS probe, never started — the gate it feeds must not be silently disarmed
    lfs = StalledRunner(":.gitattributes")
    unknown_lfs = asyncio.run(CloneWorker(runner=lfs).run(ctx, payload))  # type: ignore[arg-type]
    assert lfs.stalled and unknown_lfs.status != "ok"
    assert unknown_lfs.output is None, "has_lfs=False was published for a probe that never ran"
    assert unknown_lfs.error is not None and unknown_lfs.error.retryable is True
    assert unknown_lfs.error.failure_class is not FailureClass.TIMEOUT
    assert unknown_lfs.error.failure_class is FailureClass.TRANSIENT_INFRA

    # (c) the blob scan, never started. It calls `util.proc.run` directly (`log_dir` is not on the
    # `CommandRunner` Protocol), so the module attribute is what a test can reach.
    from fleet.util.proc import ProcResult

    async def never_scans(argv: Any, **kwargs: Any) -> ProcResult:
        return ProcResult(
            argv=tuple(argv),
            exit_code=124,
            stdout_tail="",
            stderr_tail="deadline had already passed; process was not started",
            duration_ms=0,
            timed_out=True,
            started=False,
            cwd=kwargs.get("cwd"),
        )

    monkeypatch.setattr("fleet.workers.clone.proc_run", never_scans)
    unscanned = asyncio.run(CloneWorker().run(ctx, payload))

    assert unscanned.status != "ok"
    assert unscanned.output is None, "largest_blob_bytes=0 was published for a scan never taken"
    assert unscanned.error is not None and unscanned.error.retryable is True
    assert unscanned.error.failure_class is not FailureClass.TIMEOUT, (
        "a blob scan that was never spawned was reported as one that ran too long"
    )
    assert unscanned.error.failure_class is FailureClass.TRANSIENT_INFRA
    assert RetryPolicy().decide(LadderState(), unscanned.error).charges_attempt is False


def test_the_clone_and_build_classifiers_agree_on_every_clock_failure() -> None:
    """The identical `ProcResult` must cost a repo the identical thing in either worker.

    Why it matters, corrected per review-36 I4: `44d5550` reordered
    `buildverify.classify_build_failure`'s two branches (`not started` before `timed_out`) and, in
    the SAME hunk, added a comment claiming `clone.py`'s `_no_verdict` "draws the same line, in
    this order, for this reason" and that the two "are meant to stay in step" — without touching
    `clone._error_for`, which still read `timed_out` alone (via `_indeterminate`'s
    `GitCommandError`, which dropped `started`) and answered substantive `TIMEOUT` for a probe
    that never ran. The comment was false the instant `44d5550` committed it: before that commit
    the two answered identically (both `TIMEOUT`, both wrong, per `44d5550~1`), so the divergence
    was manufactured by the half-applied reorder, not inherited from a pre-existing mismatch. Once
    manufactured: a wave deadline passing while a repo sat in `_preflight` charged that repo an
    ADR-0014 rung — three such waves burned all three attempts and reached
    `REQUIRES_HUMAN_INTERVENTION` having gathered zero evidence about the repo — while the build
    worker, handed the same three flags, answered free `TRANSIENT_INFRA` and charged nothing. That
    divergence held only for the window between `44d5550` and this fix (`68a41ff`); no code that
    ran a real wave ever saw it.

    Neither module had a test that could see the disagreement, because every test looked at one
    module. This one looks at both, so a comment can no longer be the only thing asserting it.

    **Scope is the clock, deliberately.** `(started=True, timed_out=False)` is a process that
    reached a verdict of its own, and there the two SHOULD differ: buildverify reads Bazel's exit
    table (`BUILD_ERROR`/`TEST_FAILURE`/exit 4/125), clone has no such table and reports
    `TRANSIENT_INFRA`. What is asserted for that row is only that neither invents a clock failure.

    **`retryable` is pinned directly, not via equality with `built`/`expected` (review-36 M2
    fix).** The original three-way tuple equality compared `cloned.retryable` against `built[1]`
    and `expected[1]`, but `built` and `expected` are both computed from the SAME
    `clock_failure(...)` call — `classify_build_failure` returns it verbatim — so they always move
    together, and comparing clone's value to two numbers guaranteed equal to each other cannot
    isolate a clone-specific regression. `clone._error_for` hardcodes `retryable=True` for every
    clock failure (`clone.py:716`) and never reads `clock_failure`'s own retryable half
    (`failure_class, _ = clock_failure(...)` at `clone.py:710`) — that hardcoding matches
    `clock_failure`'s current answer for both its branches, so it is not wrong today, but nothing
    failed if the literal were flipped. The assertion below now pins `cloned.retryable is True`
    directly, so flipping that literal is what makes it fail.
    """
    from fleet.util.proc import ProcResult
    from fleet.workers.base import clock_failure
    from fleet.workers.buildverify import BUILD_UNIT, classify_build_failure
    from fleet.workers.clone import _indeterminate

    def proc(*, started: bool, timed_out: bool, exit_code: int) -> ProcResult:
        return ProcResult(
            argv=("git", "rev-parse", "--verify", "--quiet", "main^{commit}"),
            exit_code=exit_code,
            stdout_tail="",
            stderr_tail="",
            duration_ms=0,
            timed_out=timed_out,
            started=started,
            cwd=Path("/nonexistent"),
        )

    shapes = [
        # exactly what `util.proc.run` synthesises for a deadline that had already passed
        proc(started=False, timed_out=True, exit_code=124),
        # …and for a process it spawned and then killed at the deadline
        proc(started=True, timed_out=True, exit_code=-9),
        proc(started=True, timed_out=True, exit_code=124),
    ]
    worker = CloneWorker()
    for result in shapes:
        expected = clock_failure(started=result.started, timed_out=result.timed_out)
        assert expected is not None, "this shape is meant to BE a clock failure"

        # `_error_for` is private and is exactly the seam under test: it is where the two
        # flags become a `FailureClass`, and no public surface exposes that step alone.
        cloned = worker._error_for(
            _indeterminate(result, "the probe produced no answer", cwd=Path("/nonexistent"))
        )
        built = classify_build_failure(result, unit=BUILD_UNIT)

        assert cloned.failure_class == built[0] == expected[0], (
            f"clone and buildverify disagree on failure_class for started={result.started} "
            f"timed_out={result.timed_out}: clone says {cloned.failure_class}, buildverify says "
            f"{built[0]}"
        )
        # Pinned directly rather than by comparing to `built`/`expected` — see the docstring's M2
        # note: those two are guaranteed to agree with each other regardless of what
        # `clone._error_for` does, so only a direct assertion on clone's own output can catch a
        # regression in it.
        assert cloned.retryable is True, (
            "clone._error_for must report every clock failure as retryable — it has no exit-code "
            "table to say otherwise, unlike buildverify"
        )
        assert built[1] == expected[1], (
            f"buildverify's retryable disagrees with clock_failure's own answer for "
            f"started={result.started} timed_out={result.timed_out}"
        )
        # The disagreement's whole cost, stated as the thing an operator pays: a rung.
        charged = RetryPolicy().decide(LadderState(), cloned).charges_attempt
        assert charged is (expected[0] is FailureClass.TIMEOUT), (
            "a never-started probe must cost no rung, and a real deadline kill must cost one"
        )

    # The boundary itself: a process that finished is nobody's clock failure, in either module.
    finished = proc(started=True, timed_out=False, exit_code=1)
    assert clock_failure(started=True, timed_out=False) is None
    assert classify_build_failure(finished, unit=BUILD_UNIT)[0] is not FailureClass.TIMEOUT
    assert (
        worker._error_for(
            _indeterminate(finished, "git said no", cwd=Path("/nonexistent"))
        ).failure_class
        is not FailureClass.TIMEOUT
    )


def test_a_credential_in_the_clone_url_reaches_git_and_nothing_else(tmp_path: Path) -> None:
    """The token goes to `git` on argv and to NO log line, output field, or `WorkerError`.

    Why it matters: every mirror in this environment embeds a plaintext `github_pat_…` in its
    origin URL, and §12.20 greps `logs/`, `artifacts/` and `migration_state.json` for that
    literal, requiring zero hits. The failure path is tested as well as the success path because
    an exception message is where the careful redaction of the happy path usually leaks.
    """
    assert credential_free(f"https://oauth2:{TOKEN}@example.invalid/a.git") == (
        "https://example.invalid/a.git"
    )

    source = make_source_repo(tmp_path / "src-repo")
    log = RecordingLog()
    ctx = make_ctx(tmp_path / "work" / "acme-billing", log=log)
    recorder = RecordingRunner()
    payload = CloneInput(
        repo_id="acme-billing",
        url=f"file://{source}",
        cache_dir=str(tmp_path / "cache"),
    )
    ok = asyncio.run(CloneWorker(runner=recorder).run(ctx, payload))  # type: ignore[arg-type]
    assert ok.status == "ok"

    # Now the same worker against a credential-bearing URL whose fetch fails, with a runner that
    # hands back an UNREDACTED stderr — the shape a hostile transport really produces.
    class LeakyRunner:
        def __init__(self) -> None:
            self.saw_token = False

        async def __call__(self, argv: Any, **kwargs: Any) -> Any:
            from fleet.util.proc import ProcResult

            self.saw_token = self.saw_token or any(TOKEN in part for part in argv)
            return ProcResult(
                argv=tuple(argv),
                exit_code=128,
                stdout_tail="",
                stderr_tail=f"fatal: could not read from https://oauth2:{TOKEN}@h/a.git",
                duration_ms=1,
                timed_out=False,
            )

    leaky = LeakyRunner()
    leak_log = RecordingLog()
    leak_ctx = make_ctx(tmp_path / "work" / "leaky", log=leak_log)
    leak_payload = CloneInput(
        repo_id="leaky-repo",
        url=f"https://oauth2:{TOKEN}@example.invalid/a.git",
        cache_dir=str(tmp_path / "cache2"),
    )
    failed = asyncio.run(CloneWorker(runner=leaky).run(leak_ctx, leak_payload))  # type: ignore[arg-type]

    assert leaky.saw_token, "git never received the credential; the test proves nothing"
    assert failed.status == "failed"
    assert failed.error is not None
    assert TOKEN not in failed.error.stderr_tail
    assert "«redacted:" in failed.error.stderr_tail
    assert TOKEN not in leak_log.rendered()
    assert TOKEN not in log.rendered()
    assert TOKEN not in (ok.output.url if ok.output else "")


def test_on_cancel_removes_half_created_worktree_debris_only(tmp_path: Path) -> None:
    """`on_cancel` cleans what the worker owns, and leaves what the next attempt will reuse.

    Why it matters: a killed `git worktree add` leaves a directory with no `.git`, and the next
    attempt's `worktree add` refuses a path that already exists — the repo would be stuck on
    debris forever. Deleting a VALID worktree would be worse: it throws away the checkout the
    resume was about to re-enter.
    """
    debris = tmp_path / "debris"
    (debris / "src").mkdir(parents=True)
    asyncio.run(CloneWorker().on_cancel(make_ctx(debris)))
    assert not debris.exists()

    result, _, ctx, _ = clone_once(tmp_path)
    assert result.status == "ok"
    asyncio.run(CloneWorker().on_cancel(ctx))
    assert Path(ctx.workdir, "pyproject.toml").is_file(), "a valid worktree was destroyed"


# =======================================================================================
# (3) interrogate — the walk, the unknown fallback, and re-entry
# =======================================================================================


def worktree_with(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    """A plain directory standing in for a cut worktree: steps 2–4 only read files."""
    root = tmp_path / "tree"
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in (files if files is not None else SOURCES).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def test_interrogate_claims_only_real_manifests_and_normalizes_coordinates(
    tmp_path: Path,
) -> None:
    """One walk, one dispatch table (ADR-0005) — and the catch-all adapter claims NO file.

    Why it matters: `UnknownAdapter.matches` returns True for every path, so a worker that
    offered each file to `adapter_for` and kept the answer would record `README.md` and every
    `.py` file as a manifest. The `manifests` table would then be the file listing, and every
    dependency edge derived from it noise.
    """
    root = worktree_with(tmp_path)
    ctx = make_ctx(root)
    payload = InterrogateInput(repo_id="acme-billing")

    result = asyncio.run(InterrogateWorker().run(ctx, payload))

    assert result.status == "ok"
    assert result.output is not None
    assert [m.path for m in result.output.manifests] == ["pyproject.toml"]
    assert result.output.ecosystems == (Ecosystem.PYPI,)
    keys = {dep.coordinate.key for dep in result.output.dependencies}
    assert "pypi::acme-commons" in keys
    assert result.completed_units == ["pyproject.toml"]


def test_interrogate_preconditions_flip_with_the_checkpoint_and_the_tree(tmp_path: Path) -> None:
    """False for fresh work; True once a checkpoint exists AND its manifests are still on disk.

    Why it matters: the checkpoint names paths. If one is gone, the persisted `manifests` rows
    describe a tree that no longer exists, and re-entering incrementally would leave the fleet's
    inventory permanently disagreeing with the worktree — so the phase must re-run whole.
    """
    root = worktree_with(tmp_path)
    ctx = make_ctx(root)
    worker = InterrogateWorker()

    fresh = InterrogateInput(repo_id="acme-billing")
    assert asyncio.run(worker.preconditions_hold(ctx, fresh)) is False

    done = InterrogateInput(
        repo_id="acme-billing", remaining_units=(), completed_units=("pyproject.toml",)
    )
    assert asyncio.run(worker.preconditions_hold(ctx, done)) is True

    (root / "pyproject.toml").unlink()
    assert asyncio.run(worker.preconditions_hold(ctx, done)) is False

    gone_tree = done.model_copy(update={"worktree_path": str(tmp_path / "never")})
    assert asyncio.run(worker.preconditions_hold(ctx, gone_tree)) is False


def test_interrogate_reentry_with_nothing_owed_parses_nothing(tmp_path: Path) -> None:
    """A completed phase re-entered is a no-op: no manifest is parsed a second time.

    Why it matters: re-parsing is not merely wasted work — every re-parse emits another set of
    `manifests`/`coordinates` rows for the runner to write, which is how a resumed run doubles
    its own inventory.
    """
    root = worktree_with(tmp_path)
    ctx = make_ctx(root)
    first = asyncio.run(InterrogateWorker().run(ctx, InterrogateInput(repo_id="acme-billing")))
    assert first.output is not None and len(first.output.manifests) == 1

    again = asyncio.run(
        InterrogateWorker().run(
            ctx, InterrogateInput(repo_id="acme-billing", remaining_units=())
        )
    )

    assert again.status == "ok"
    assert again.output is not None
    assert again.output.manifests == ()
    assert again.completed_units == ["pyproject.toml"]


def test_interrogate_synthesizes_one_manifest_for_an_unknown_ecosystem(tmp_path: Path) -> None:
    """§3.1 step 2's first-class unknown path: one `ManifestRef` at `.`, and a `no-manifest`.

    Why it matters: success criterion (a) is that a repo with no recognizable manifest still
    appears in the fleet with a finding. A repo silently dropped here never reaches a wave, never
    gets a PR, and nobody finds out until the monorepo is missing a service.
    """
    root = worktree_with(tmp_path, {"README.md": "# nothing to see\n", "main.rb": "puts 1\n"})
    result = asyncio.run(
        InterrogateWorker().run(make_ctx(root), InterrogateInput(repo_id="acme-billing"))
    )

    assert result.status == "ok"
    assert result.output is not None
    assert len(result.output.manifests) == 1
    only = result.output.manifests[0]
    assert only.path == "."
    assert only.adapter == "unknown"
    assert only.low_confidence is True
    assert only.publishes is not None and only.publishes.name == "acme-billing"
    assert result.output.findings == ("no-manifest",)


def test_the_unknown_ecosystem_repo_and_its_advisory_kind_are_two_different_facts(
    tmp_path: Path,
) -> None:
    """§12.25's fixture repo (`README.md` + a shell script, no recognizable manifest) proves two
    genuinely different fields, not one read twice under two names.

    `Ecosystem.UNKNOWN` is STRUCTURAL: no `ManifestAdapter` matched this repo's file layout, so
    `interrogate._unknown_manifest` synthesized the §3.1 step 2 fallback `ManifestRef`. `repos.kind`
    is ADVISORY: the classify worker's (ADR-0008) judgment about what *kind* of thing the repo is,
    derived purely from the model's own confidence — `_kind_for` never reads the ecosystem the
    model was told about. A repo can be structurally UNKNOWN and still get a confident,
    non-`'unknown'` `kind` if the model is confident about the (thin) evidence; SPEC §12.25's
    sentence describes a fixture where both readings happen to coincide, not one field read twice.

    Each assertion below is proven a real, independent discriminator (Rule 12): commenting out
    `_kind_for`'s `if not trusted: return "unknown"` branch reddens ONLY the advisory assertion;
    pointing `_unknown_manifest`'s ecosystem at the wrong `Ecosystem` member reddens ONLY the
    structural one. Verified by hand, not asserted by this test — see the task report.
    """
    root = worktree_with(
        tmp_path, {"README.md": "# nothing to see\n", "deploy.sh": "#!/bin/sh\necho ok\n"}
    )

    interrogated = asyncio.run(
        InterrogateWorker().run(make_ctx(root), InterrogateInput(repo_id="acme-billing"))
    )
    assert interrogated.status == "ok"
    assert interrogated.output is not None
    only = interrogated.output.manifests[0]

    # STRUCTURAL: no manifest adapter matched this repo's file layout.
    assert only.publishes is not None
    assert only.publishes.ecosystem is Ecosystem.UNKNOWN

    client = FakeModelClient(
        RepoClassification(
            ecosystem=Ecosystem.UNKNOWN,
            is_library=False,
            confidence=0.2,  # below ClassifyInput.min_confidence's 0.5 floor: distrusted
            rationale="a README and a shell script are thin evidence",
        )
    )
    classified = asyncio.run(ClassifyWorker().run(make_ctx(root, llm=client), classify_payload()))
    assert classified.status == "ok"
    assert classified.output is not None

    # ADVISORY: the classify worker's confidence gate downgraded a distrusted answer — this
    # branch never consults `only.publishes.ecosystem` or any other structural fact.
    assert classified.output.kind == "unknown"


def test_interrogate_keeps_an_unparsable_manifest_as_low_confidence(tmp_path: Path) -> None:
    """A manifest that will not parse is recorded with its error, never dropped.

    Why it matters: "parsed to zero dependencies" and "could not be read" are indistinguishable
    downstream, and the difference is every graph edge the file declared. `low_confidence` is
    also what routes it to the ADR-0008 extraction slot.
    """
    root = worktree_with(tmp_path, {"pyproject.toml": "this is not toml ][\n"})
    result = asyncio.run(
        InterrogateWorker().run(make_ctx(root), InterrogateInput(repo_id="acme-billing"))
    )

    assert result.status == "ok"
    assert result.output is not None
    ref = result.output.manifests[0]
    assert ref.low_confidence is True
    assert ref.parse_error is not None and "pyproject.toml" in ref.parse_error
    assert ref.dependency_count == 0


# =======================================================================================
# (4) symbolindex — the headline: partial on deadline, resume without re-indexing
# =======================================================================================


def index_payload(**overrides: Any) -> SymbolIndexInput:
    return SymbolIndexInput(repo_id="acme-billing", **overrides)


def test_symbolindex_extracts_definitions_imports_and_proto_identity(tmp_path: Path) -> None:
    """The rows Constraint 4's cross-repo joins need: definitions, imports, and a proto package.

    Why it matters: `INTERNAL_IMPORT` edges are inferred by matching an import symbol in A
    against a coordinate owned by B, and `API_CONTRACT`/§3.1 5b key on the `.proto` package. A
    missing import row is an undeclared dependency the fleet cannot see — exactly the class of
    edge a single-repo scan misses.
    """
    root = worktree_with(tmp_path)
    result = asyncio.run(SymbolindexWorker().run(make_ctx(root), index_payload()))

    assert result.status == "ok"
    assert result.output is not None
    by_fqn = {(s.fqn, s.kind.value) for s in result.output.symbols}
    assert ("src.acme.service.BillingService", "class") in by_fqn
    assert ("src.acme.service.charge", "function") in by_fqn
    assert ("acme.commons", "import") in by_fqn
    assert ("acme.ledger", "import") in by_fqn
    assert ("acme.billing.v1", "module") in by_fqn
    assert ("acme.billing.v1.Billing", "grpc_service") in by_fqn
    assert ("acme.billing.v1.Invoice", "proto_message") in by_fqn
    assert ("openapi.yaml#openapi", "module") in by_fqn, "5b needs the YAML root keys"
    assert all(symbol.line >= 1 for symbol in result.output.symbols)


def test_symbolindex_reports_partial_with_completed_units_when_the_deadline_expires(
    tmp_path: Path,
) -> None:
    """THE reason `WorkerResult` is five-valued: interrupted work is `partial`, never `failed`.

    Why it matters: with `failed` the attempt-2 dispatch would re-parse every file and re-emit
    every row the first attempt already landed, doubling the `symbols` table for that repo. The
    checkpoint's `completed_units` is what makes the second pass start where the first stopped.
    """
    root = worktree_with(tmp_path)
    ctx = make_ctx(root)
    pool = SyncPool(ctx=ctx, expire_after=1)
    ctx.limits.cpu_pool = pool  # type: ignore[attr-defined]

    result = asyncio.run(SymbolindexWorker().run(ctx, index_payload()))

    assert result.status == "partial", result.error
    assert result.completed_units, "partial with nothing completed is `failed` in disguise"
    assert result.remaining_units
    assert not set(result.completed_units) & set(result.remaining_units)
    assert result.output is not None and result.output.symbols
    assert len(pool.parsed) == 1


def test_symbolindex_resume_indexes_only_the_owed_files(tmp_path: Path) -> None:
    """Re-entry parses `remaining_units` and NOT one file more — no duplicated symbol rows.

    Why it matters: this is the other half of the `partial` contract. A resume that re-walked the
    whole tree would insert a second copy of every row the interrupted attempt landed, and the
    duplicates are indistinguishable from real symbols once they are in the table.
    """
    root = worktree_with(tmp_path)
    ctx = make_ctx(root)
    first_pool = SyncPool(ctx=ctx, expire_after=1)
    ctx.limits.cpu_pool = first_pool  # type: ignore[attr-defined]
    first = asyncio.run(SymbolindexWorker().run(ctx, index_payload()))
    assert first.status == "partial"

    resume_ctx = make_ctx(root)
    resume_pool = SyncPool()
    resume_ctx.limits.cpu_pool = resume_pool  # type: ignore[attr-defined]
    resumed = asyncio.run(
        SymbolindexWorker().run(
            resume_ctx, index_payload(remaining_units=tuple(first.remaining_units))
        )
    )

    assert resumed.status == "ok"
    assert set(resume_pool.parsed).isdisjoint(set(first_pool.parsed)), "a landed file was re-read"
    assert sorted(resume_pool.parsed) == sorted(first.remaining_units)
    assert first.output is not None and resumed.output is not None
    landed = [(s.repo_id, s.fqn, s.path, s.line) for s in first.output.symbols]
    landed += [(s.repo_id, s.fqn, s.path, s.line) for s in resumed.output.symbols]
    assert len(landed) == len(set(landed)), "the two passes produced duplicate symbol rows"


def test_symbolindex_reentry_with_nothing_owed_reads_no_file(tmp_path: Path) -> None:
    """A completed index re-entered indexes nothing: `ok`, zero files, zero rows.

    Why it matters: "re-running a completed worker is a no-op" is the property the whole resume
    contract rests on, and the observable side effect is whether the pool was handed any file.
    """
    root = worktree_with(tmp_path)
    ctx = make_ctx(root)
    pool = SyncPool()
    ctx.limits.cpu_pool = pool  # type: ignore[attr-defined]

    result = asyncio.run(SymbolindexWorker().run(ctx, index_payload(remaining_units=())))

    assert result.status == "ok"
    assert pool.parsed == []
    assert result.output is not None and result.output.symbols == ()
    assert result.output.files_indexed == 0


def test_symbolindex_preconditions_require_the_checkpointed_files_to_still_exist(
    tmp_path: Path,
) -> None:
    """False fresh, True once landed, False again when a landed file disappears.

    Why it matters: the index is a claim about files. If one is gone the persisted rows are
    stale, and admitting re-entry would leave them stale forever — the phase must restart from
    `phases.base_ref` instead.
    """
    root = worktree_with(tmp_path)
    ctx = make_ctx(root)
    worker = SymbolindexWorker()

    indexed = index_payload(
        remaining_units=(), completed_units=("src/acme/service.py", "api/billing.proto")
    )
    assert asyncio.run(worker.preconditions_hold(ctx, index_payload())) is False
    assert asyncio.run(worker.preconditions_hold(ctx, indexed)) is True

    (root / "src" / "acme" / "service.py").unlink()
    assert asyncio.run(worker.preconditions_hold(ctx, indexed)) is False


def test_symbolindex_stops_between_files_when_cancelled(tmp_path: Path) -> None:
    """Cancellation is honoured at a unit boundary, with what landed reported as `partial`.

    Why it matters: `CancelledError` never reaches a pool child (§11.1), so cooperation is the
    only mechanism there is. A worker that ignored `ctx.cancel` would keep a core busy after the
    wave that owns it has been told to stop, and the reaper would delete the tree underneath it.
    """
    root = worktree_with(tmp_path)
    ctx = make_ctx(root)

    class CancellingPool(SyncPool):
        def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Future[Any]:
            future = super().submit(fn, *args, **kwargs)
            ctx.cancel.set()
            return future

    pool = CancellingPool()
    ctx.limits.cpu_pool = pool  # type: ignore[attr-defined]

    result = asyncio.run(SymbolindexWorker().run(ctx, index_payload()))

    assert result.status == "partial"
    assert len(pool.parsed) == 1, "the worker kept working after cancellation"
    assert result.remaining_units


def test_symbolindex_batch_ceiling_hands_over_rather_than_growing(tmp_path: Path) -> None:
    """A full batch is handed back as `partial` instead of accumulating the whole repo (§11.3).

    Why it matters: the memory bound is the point — "one file in, rows out, drop the batch". A
    worker that returned every row for a 500 000-symbol repo would hold the fleet's largest table
    in RAM, which §11.3 forbids by construction rather than by convention.
    """
    root = worktree_with(tmp_path)
    result = asyncio.run(
        SymbolindexWorker().run(make_ctx(root), index_payload(symbol_batch_rows=1))
    )

    assert result.status == "partial"
    assert result.output is not None and len(result.output.symbols) >= 1
    assert result.remaining_units


def test_symbolindex_fails_loud_when_the_worktree_is_missing(tmp_path: Path) -> None:
    """No worktree is a structured `PREFLIGHT` failure, not an empty index reported as success.

    Why it matters: an empty index that says `ok` is the worst outcome available — every
    `INTERNAL_IMPORT` edge for that repo silently disappears and the DAG under-orders the wave.
    """
    result = asyncio.run(
        SymbolindexWorker().run(make_ctx(tmp_path / "gone"), index_payload())
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.failure_class is FailureClass.PREFLIGHT
    assert result.error.retryable is False


# --------------------------------------------------------------------------------------
# D40 — `worktree_presence` is the house decoder for "is the clone worktree there", and it must
# distinguish a genuine absence (`Path.is_dir()`'s old `False`) from an indeterminate `stat`
# failure that `Path.is_dir()` used to swallow into the identical `False`. There is no
# `ProcResult` here — it is a syscall, not a subprocess — so the fix shape mirrors `no_verdict`
# without reusing it.
# --------------------------------------------------------------------------------------
def test_worktree_presence_distinguishes_absent_from_indeterminate(tmp_path: Path) -> None:
    """The three real answers `os.stat` can give: present, genuinely absent, and (for a path
    under a file, not a directory) absent for a different but still SETTLED reason."""
    present = tmp_path / "present"
    present.mkdir()
    assert worktree_presence(present) is True

    absent = tmp_path / "absent"
    assert worktree_presence(absent) is False

    a_file = tmp_path / "a-file"
    a_file.write_text("not a directory")
    assert worktree_presence(a_file) is False

    # NotADirectoryError: a path component is a file, not a directory — still a real "no".
    assert worktree_presence(a_file / "child") is False


def test_worktree_presence_reports_an_unclassifiable_oserror_rather_than_a_bare_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EACCES on a parent, ELOOP, a stale NFS handle: none of these mean "the worktree is not
    there", and `stat` raising something other than `FileNotFoundError`/`NotADirectoryError`
    must come back AS the exception, not collapsed into the same `False` a genuine absence gets —
    that collapse is exactly what D40 files against `Path.is_dir()`."""
    root = tmp_path / "wt"
    root.mkdir()

    def raiser(self: Path, *args: Any, **kwargs: Any) -> Any:
        raise PermissionError(f"synthetic EACCES for {self}")

    monkeypatch.setattr(Path, "stat", raiser)

    result = worktree_presence(root)

    assert isinstance(result, OSError)
    assert not isinstance(result, FileNotFoundError | NotADirectoryError)


def test_symbolindex_worktree_check_that_cannot_settle_is_retryable_not_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before the fix, `Path.is_dir()` reported an EACCES/stale-handle/ELOOP fault identically to
    a repo that was never cloned: non-retryable `PREFLIGHT`, straight to
    `REQUIRES_HUMAN_INTERVENTION` with no retry, for a condition the very next attempt could
    plausibly clear. The probe that never settled must come back retryable — `TRANSIENT_INFRA`,
    the same class `clock_failure` uses for a subprocess call that gathered no evidence — not the
    terminal gate reserved for a worktree that is genuinely not there.
    """
    root = tmp_path / "wt"
    root.mkdir()
    real_stat = Path.stat

    def flaky_stat(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == root:
            raise PermissionError(f"synthetic EACCES for {self}")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)

    result = asyncio.run(SymbolindexWorker().run(make_ctx(root), index_payload()))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.failure_class is not FailureClass.PREFLIGHT, (
        "an indeterminate stat failure must not be reported as the repo's own shape"
    )
    assert result.error.failure_class is FailureClass.TRANSIENT_INFRA
    assert result.error.retryable is True


def test_interrogate_fails_loud_when_the_worktree_is_missing(tmp_path: Path) -> None:
    """The negative control paired with the test below: a GENUINE absence must still produce the
    old, terminal behaviour unchanged — proving the fix did not turn every negative into a retry."""
    result = asyncio.run(
        InterrogateWorker().run(
            make_ctx(tmp_path / "gone"), InterrogateInput(repo_id="acme-billing")
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.failure_class is FailureClass.PREFLIGHT
    assert result.error.retryable is False


def test_interrogate_worktree_check_that_cannot_settle_is_retryable_not_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`interrogate.py`'s `run()` has the identical D40 shape as `symbolindex.py`'s, off the same
    shared `worktree_presence` — pinned separately so a future divergence between the two call
    sites is caught here, not assumed from symbolindex's coverage alone."""
    root = tmp_path / "wt"
    root.mkdir()
    real_stat = Path.stat

    def flaky_stat(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == root:
            raise PermissionError(f"synthetic EACCES for {self}")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)

    result = asyncio.run(
        InterrogateWorker().run(make_ctx(root), InterrogateInput(repo_id="acme-billing"))
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.failure_class is not FailureClass.PREFLIGHT
    assert result.error.failure_class is FailureClass.TRANSIENT_INFRA
    assert result.error.retryable is True


# =======================================================================================
# (5) classify — through the ModelClient, validated, and typed when it is not
# =======================================================================================


class FakeModelClient:
    """A `ModelClient` that answers from a canned reply, or raises the way the real one does."""

    def __init__(self, answer: RepoClassification | None = None, error: Exception | None = None):
        self.answer = answer
        self.error = error
        self.calls: list[tuple[str, list[Message]]] = []

    async def complete(
        self, role: str, messages: Any, response_model: Any, **kwargs: Any
    ) -> ModelResponse[Any]:
        self.calls.append((role, list(messages)))
        if self.error is not None:
            raise self.error
        assert response_model is RepoClassification, "classify must ask for its own schema"
        return ModelResponse(
            value=self.answer,
            usage=TokenUsage(role=role, input_tokens=120, output_tokens=30),
            mode=StructuredOutputMode.JSON_SCHEMA,
            finish_reason="stop",
        )


def classify_payload(**overrides: Any) -> ClassifyInput:
    return ClassifyInput(repo_id="acme-billing", **overrides)


def test_classify_routes_through_the_injected_client_and_validates_the_reply(
    tmp_path: Path,
) -> None:
    """The judgment is the model's; the evidence and the label mapping are code's (Rule 5).

    Why it matters: routing through `ModelClient` is what keeps a vendor SDK out of the
    orchestration path (ADR-0023), and validating into `RepoClassification` is what stops an
    invented field or a missing confidence from being treated as an answer.
    """
    root = worktree_with(tmp_path)
    client = FakeModelClient(
        RepoClassification(
            ecosystem=Ecosystem.PYPI,
            is_library=True,
            confidence=0.9,
            rationale="publishes a coordinate consumed elsewhere",
        )
    )
    worker = ClassifyWorker()

    result = asyncio.run(
        worker.run(
            make_ctx(root, llm=client),
            classify_payload(manifest_paths=("pyproject.toml",)),
        )
    )

    assert result.status == "ok"
    assert len(client.calls) == 1
    role, messages = client.calls[0]
    assert role == "repo_classify"
    assert "pyproject.toml" in messages[-1].content
    assert result.output is not None
    assert result.output.kind == "library"
    assert result.output.confidence == 0.9
    assert result.output.low_confidence is False
    assert result.completed_units == [UNIT]
    assert result.usage.input_tokens == 120


def test_classify_downgrades_a_low_confidence_answer_instead_of_trusting_it(
    tmp_path: Path,
) -> None:
    """Below the floor the label is `unknown` — advisory metadata, never a fact (ADR-0008).

    Why it matters: §3.1 gates on confidence. A guess promoted to a fact would let a CHEAP-tier
    label influence how a reviewer reads a PR, and the schema makes the number mandatory
    precisely so it cannot be dropped on the way through.
    """
    root = worktree_with(tmp_path)
    client = FakeModelClient(
        RepoClassification(
            ecosystem=Ecosystem.UNKNOWN, is_library=True, confidence=0.2, rationale="thin evidence"
        )
    )

    result = asyncio.run(ClassifyWorker().run(make_ctx(root, llm=client), classify_payload()))

    assert result.status == "ok"
    assert result.output is not None
    assert result.output.kind == "unknown"
    assert result.output.low_confidence is True


def test_classify_turns_a_malformed_reply_into_a_structured_worker_error(
    tmp_path: Path,
) -> None:
    """A reply that never satisfied the schema is a typed `PARSE_ERROR`, not a crashed wave.

    Why it matters: LLM output is untrusted text until it validates. An exception escaping here
    would be classified by the generic fallback as `UNKNOWN`, and `retry.py` would have message
    text rather than a `failure_class` to branch on.
    """
    root = worktree_with(tmp_path)
    target = BackendTarget(backend="fake", model_id="fake-cheap", price="free")
    client = FakeModelClient(error=SchemaUnsatisfied(target, 2, "missing field `confidence`"))

    result = asyncio.run(ClassifyWorker().run(make_ctx(root, llm=client), classify_payload()))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.failure_class is FailureClass.PARSE_ERROR
    assert result.error.retryable is True
    assert result.error.exception_type == "fleet.llm.client.SchemaUnsatisfied"
    assert result.output is None


def test_classify_takes_no_model_client_by_constructor_and_calls_the_one_on_the_context(
    tmp_path: Path,
) -> None:
    """REPLACES `test_classify_without_a_client_is_a_wiring_failure_not_a_silent_default`.

    That test asserted the old workaround: the client was a CONSTRUCTOR argument, the registry
    instantiates workers with no arguments, so a perfectly healthy run reported
    `BACKEND_UNAVAILABLE` because nobody had passed one in. The client is now on the context, so
    the mis-wired state it described is unrepresentable — and the two facts worth asserting are
    that it is unrepresentable and that the ONE remaining unreachable-model state (§11.8: every
    target for the tier is unhealthy) is still terminal and named rather than a fabricated
    `unknown` label a whole fleet would inherit.
    """
    assert "client" not in inspect.signature(ClassifyWorker).parameters, (
        "a constructor-injected client would be a second authority for `ctx.llm`"
    )

    result = asyncio.run(
        ClassifyWorker().run(make_ctx(worktree_with(tmp_path)), classify_payload())
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.failure_class is FailureClass.BACKEND_UNAVAILABLE
    assert result.error.retryable is False
    assert result.error.exception_type == "fleet.llm.client.TierUnavailable"
    assert result.error.tier is ModelTier.CHEAP, (
        "D78: `_error_for` must populate `WorkerError.tier` from the real "
        "`TierUnavailable.tier` this fixture raises"
    )


def test_classify_preconditions_and_reentry_spend_no_second_call(tmp_path: Path) -> None:
    """Already classified against a tree still on disk ⇒ True, and re-entry calls no model.

    Why it matters: this role runs ~250 times per fleet. A resume that re-asked for every repo
    would pay the CHEAP tier twice for an answer already persisted — and §11.2's budget is
    fail-closed, so that spend comes out of the transform budget.
    """
    root = worktree_with(tmp_path)
    client = FakeModelClient(
        RepoClassification(
            ecosystem=Ecosystem.PYPI, is_library=False, confidence=0.8, rationale="an app"
        )
    )
    ctx = make_ctx(root, llm=client)
    worker = ClassifyWorker()

    assert asyncio.run(worker.preconditions_hold(ctx, classify_payload())) is False
    assert asyncio.run(worker.run(ctx, classify_payload())).output is not None
    assert len(client.calls) == 1

    done = classify_payload(remaining_units=())
    assert asyncio.run(worker.preconditions_hold(ctx, done)) is True
    again = asyncio.run(worker.run(ctx, done))
    assert again.status == "ok"
    assert len(client.calls) == 1, "a completed classification was paid for twice"

    missing_tree = classify_payload(remaining_units=(), worktree_path=str(tmp_path / "gone"))
    assert asyncio.run(worker.preconditions_hold(ctx, missing_tree)) is False


# =======================================================================================
# (6) the ladder — a scan worker driven through `execute()` behaves like one
# =======================================================================================


def test_execute_records_a_cancelled_midway_index_as_one_resumable_attempt(
    tmp_path: Path,
) -> None:
    """Driven through the real ladder, an interrupted index is one attempt and stays resumable.

    Why it matters: `execute()` treats `partial` as landed work worth an attempt and leaves the
    repo `PENDING`. If a scan worker's `partial` were mistaken for a failure, the ladder would
    escalate a deterministic file walk to an LLM rung — which cannot help and costs money — and
    the rows it already landed would be replayed on the way there.

    The interruption is driven through `ctx.cancel` rather than the deadline on purpose:
    `execute()` hands `run()` a `dataclasses.replace` COPY of the context, so a mutated
    `deadline` float would not reach the worker, while the `asyncio.Event` is shared by
    reference — which is precisely why cancellation is the mechanism §7.1 makes cooperative.
    """

    class NoPhase:
        async def get_phase(self, *_: Any, **__: Any) -> None:
            return None

    root = worktree_with(tmp_path)
    ctx = make_ctx(root)
    ctx.db = NoPhase()  # type: ignore[assignment]

    class CancelAfterFirst(SyncPool):
        def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Future[Any]:
            future = super().submit(fn, *args, **kwargs)
            ctx.cancel.set()
            return future

    pool = CancelAfterFirst()
    ctx.limits.cpu_pool = pool  # type: ignore[attr-defined]

    execution = asyncio.run(SymbolindexWorker().execute(ctx, index_payload(), max_attempts=1))

    assert execution.attempts == 1
    assert execution.status.value == "PENDING"
    assert execution.final is not None
    assert execution.final.status == "partial"
    assert execution.final.completed_units
    assert len(pool.parsed) == 1


# =======================================================================================
# (N) the disk floor — §11.3, re-checked before EVERY clone
# =======================================================================================

IMPOSSIBLE_FLOOR = 2**62
"""A floor no volume on earth clears (~4.6 EB), so the refusal below is driven by a REAL
`statvfs` of a real directory rather than by a patched `disk_usage`. The number the harness
reports is therefore the number the kernel gave it, which is the whole point of the assertion
that the message names the actual free space."""


def test_clone_refuses_to_start_when_the_volume_is_under_the_configured_floor(
    tmp_path: Path,
) -> None:
    """§11.3: `preflight.min_free_bytes` is checked BEFORE `git clone --mirror`, every time.

    Why it matters, and why this test exists at all: the config key, the `FailureClass` and exit
    code 9 were all declared while nothing in `src/` ever read the free-space number — and then
    this project's own test suite drove the host to 0 bytes free, which is exactly the §13 row 42
    scenario (250 mirrors fill the volume, `ENOSPC` lands inside a `BEGIN IMMEDIATE`, and the
    "every non-zero exit leaves a valid checkpoint" guarantee is void). A ceiling that is only
    declared is not a ceiling.

    The refusal must be `DISK_EXHAUSTED` and non-retryable: reported as transient infra it would
    buy three more attempts at filling a volume that is already too full, and reported as
    `PREFLIGHT` it would blame the repo for the host's disk.
    """
    result, payload, _, recorder = clone_once(tmp_path, min_free_bytes=IMPOSSIBLE_FLOOR)

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.failure_class is FailureClass.DISK_EXHAUSTED
    assert result.error.retryable is False
    # The refusal names BOTH numbers. An operator who is told "not enough disk" and not how much
    # is missing cannot tell a 2 GB shortfall from a 200 GB one, and both have different fixes.
    detail = result.error.stderr_tail
    assert str(IMPOSSIBLE_FLOOR) in detail, detail
    assert str(shutil.disk_usage(tmp_path).free)[:3] in detail, detail
    assert "min_free_bytes" in detail, detail
    # …and nothing was cloned. The gate is BEFORE the operation, not a verdict on its wreckage.
    assert recorder.argvs == [], recorder.subcommands()
    assert not Path(payload.cache_dir).exists()


def test_a_clone_is_not_gated_by_a_floor_the_volume_clears(tmp_path: Path) -> None:
    """The negative control the gate is worthless without.

    Why it matters: a check that refused unconditionally would pass the test above and stop the
    harness from ever cloning anything. `min_free_bytes=1` is a real check against a real volume
    — not a disabled one — so this asserts the comparison, not the branch.
    """
    result, _, _, _ = clone_once(tmp_path, min_free_bytes=1)

    assert result.status == "ok", result.error
    assert result.output is not None and result.output.head_sha is not None


def test_the_floor_is_off_by_default_so_a_hand_built_payload_is_not_gated(tmp_path: Path) -> None:
    """`min_free_bytes` defaults to 0 = unchecked, and that is a deliberate, visible choice.

    Why it matters: the spec's floor is 50 GiB. Defaulting a worker payload to it would gate
    every test on this developer host — and, worse, would hide the wiring question, because a
    payload that gates correctly whether or not the CLI passed the configured value proves
    nothing about the CLI. Off-by-default means `fleet scan` must pass it explicitly, and the
    test below (`test_cli.py`) is what proves it does.
    """
    assert CloneInput(repo_id="r", url="file:///x", cache_dir="cache").min_free_bytes == 0
    result, _, _, _ = clone_once(tmp_path)
    assert result.status == "ok", result.error


if __name__ == "__main__":  # pragma: no cover - convenience only
    raise SystemExit(pytest.main([__file__, "-q"]))
