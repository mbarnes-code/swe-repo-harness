"""Phase 2's transform workers against real git repos (SPEC §3.2, §7.1).

Every test here is a bill someone would otherwise pay twice:

* **`partial` is load-bearing.** 40 of 60 units land, the deadline arrives, and re-entry must not
  redo the 40 — asserted on the *commit log*, because the returned lists are a claim and the log
  is the fact (ADR-0024: the commit IS the record).
* **The guard is two conditions, and the second one decides.** A trailer proves a patch was once
  committed; it does not prove the effect survived. A trailer-only guard permanently skipped a
  patch whose hunk a rebase had dropped, and the PR shipped pointing at a path that no longer
  existed. Both halves are asserted: skipped when present, re-applied when reverted.
* **`relocate` may never re-run a rename over a renamed tree** — `java/java/com/x` (§7.1).
* **A `RuleConflict` is an operator's YAML defect**, so it leaves the file unchanged and spends
  no rung: `RetryPolicy.decide(...).charges_attempt is False`.
* **A repair rung sees THIS failure's verbatim stderr and no transcript** (guardrail 5): the
  previous invocation's error text, the rejected diff, and the rejected-approach summaries a
  higher policy would carry are all asserted ABSENT from the rendered prompt.
* **Rollback is per TASK.** A failing unit resets to its own anchor; the earlier units' commits
  are still on the branch, because resetting to the phase anchor would delete work whose rows are
  already `DONE`.

The engines (`ast-grep`, `libcst`, `ts-morph`) are external tools and none is a dependency, so the
pipeline is driven with the same in-test fake engine `tests/test_rewrite.py` uses; `git` is real,
and so is every commit asserted on. No network, no model: the `ModelClient` is a fake that records
the prompt it was handed.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from time import monotonic
from typing import Any, ClassVar
from uuid import UUID

import pytest
from pydantic import BaseModel

from fleet.llm.client import (
    BackendReply,
    CallBudget,
    LadderModelClient,
    Message,
    ModelResponse,
    StreamEvent,
    TierUnavailable,
)
from fleet.llm.roles import SPEC_ROLE_TIERS, LlmRouter, Role
from fleet.llm.schemas import LlmEscalationProposal, LlmPatchProposal, ProposedFileEdit
from fleet.models.enums import (
    ContextPolicy,
    FailureClass,
    ModelTier,
    Phase,
    RepoStatus,
    StructuredOutputMode,
    TransformTier,
)
from fleet.models.tasks import (
    BackendTarget,
    FilePatch,
    ModelCapabilities,
    Price,
    RejectedApproach,
    TokenUsage,
)
from fleet.orchestrator.retry import LadderState, RetryAction, RetryPolicy
from fleet.rewrite.apply import make_unified_diff
from fleet.rewrite.approach import compute_approach_signature
from fleet.rewrite.pipeline import RewritePipeline
from fleet.rewrite.rules import EngineRegistry, RewriteRule
from fleet.state.repository import PhaseRow
from fleet.workers import relocate as relocate_mod
from fleet.workers import rewrite as rewrite_mod
from fleet.workers.base import (
    BaseWorker,
    UnsafeSourcePathError,
    WorkerContext,
    assert_stateless,
    implements_preconditions,
)
from fleet.workers.relocate import RelocateInput, RelocateWorker, relocated_path
from fleet.workers.rewrite import (
    RewriteInput,
    RewriteWorker,
    WorkerRepairError,
    task_id_for,
)

RUN_ID = UUID("00000000-0000-4000-8000-0000000c0ffe")
REPO_ID = "acme-billing"
BRANCH = "migrate/acme-billing"
DEST = "java/com/acme/billing"
OWNER = "host:container:99:boot"


# =======================================================================================
# fakes: an engine, a read-only repository, a model client
# =======================================================================================
class FakeRewriter:
    """A `Rewriter` whose transform is a plain `str -> str` keyed by rule id.

    Same shape as `tests/test_rewrite.py`'s, plus an optional side effect, which is how the
    rollback test simulates the untracked debris a killed `git apply` leaves behind.
    """

    engine = "fake"

    def __init__(
        self,
        transforms: Mapping[str, Callable[[str], str]],
        *,
        on_apply: Callable[[str], None] | None = None,
    ) -> None:
        self._transforms = dict(transforms)
        self._on_apply = on_apply
        self.seen: list[tuple[str, str]] = []

    async def apply(
        self, rule: RewriteRule, path: str, source: str, params: dict[str, str]
    ) -> FilePatch | None:
        self.seen.append((rule.id, path))
        if self._on_apply is not None:
            self._on_apply(path)
        transform = self._transforms.get(rule.id)
        rewritten = source if transform is None else transform(source)
        diff = make_unified_diff(path, source, rewritten)
        if not diff:
            return None
        return FilePatch(
            path=path,
            diff=diff,
            tier=TransformTier.DETERMINISTIC,
            parse_probe_ok=False,
            rule_id=rule.id,
        )

    async def parse_probe(self, path: str) -> bool:
        return True


class FakeDb:
    """`ReadOnlyRepository` narrowed to the one row `BaseWorker.execute()` reads: the lease, plus
    an in-memory `rejected_approaches` set the anchoring-guard tests seed directly (ADR-0021)."""

    def __init__(self, rejected: Mapping[str, frozenset[str]] | None = None) -> None:
        self._rejected = dict(rejected or {})

    async def get_phase(self, run_id: str, repo_id: str, phase: Phase) -> PhaseRow | None:
        return None

    async def get_rejected_approach_signatures(
        self, run_id: str, task_id: str
    ) -> frozenset[str]:
        return self._rejected.get(task_id, frozenset())


class FakeModelClient:
    """A `ModelClient` that records the rendered prompt and answers with a canned proposal.

    Recording the prompt is the whole point: guardrail 5 is a statement about what a repair rung
    is SHOWN, and the only way to check it is to read what was sent.
    """

    def __init__(self, value: BaseModel) -> None:
        self._value = value
        self.prompts: list[str] = []
        self.roles: list[str] = []

    async def complete[T: BaseModel](
        self,
        role: str,
        messages: Sequence[Message],
        response_model: type[T],
        *,
        tier_override: ModelTier | None = None,
        max_output_tokens: int | None = None,
        timeout_s: float | None = None,
        budget: CallBudget | None = None,
    ) -> ModelResponse[T]:
        self.roles.append(role)
        self.prompts.append("\n".join(message.content for message in messages))
        return ModelResponse(
            value=response_model.model_validate_json(self._value.model_dump_json()),
            usage=TokenUsage(role=role, input_tokens=100, output_tokens=20, cost_usd=0.01),
            mode=StructuredOutputMode.JSON_SCHEMA,
            finish_reason="stop",
        )

    async def _empty(self) -> AsyncIterator[StreamEvent]:
        return
        yield StreamEvent()  # pragma: no cover - never reached; makes this an async generator

    def stream[T: BaseModel](
        self,
        role: str,
        messages: Sequence[Message],
        response_model: type[T],
        *,
        budget: CallBudget | None = None,
    ) -> AsyncIterator[StreamEvent]:
        return self._empty().__aiter__()

    async def capabilities(self, role: str) -> ModelCapabilities:
        return ModelCapabilities()


class TierUnavailableClient:
    """A `ModelClient` whose tier is genuinely dead (§11.8) — a REAL `TierUnavailable` out of
    `complete()`, exactly what an exhausted tier raises. Not a hand-built exception object: this
    is `ctx.llm`, the one §7.7 call surface, and the exception crosses it for real (D133).
    """

    def __init__(
        self, tier: ModelTier, targets_tried: Sequence[str] = ("fake:fake-heavy",)
    ) -> None:
        self._tier = tier
        self._targets_tried = tuple(targets_tried)

    async def complete[T: BaseModel](
        self,
        role: str,
        messages: Sequence[Message],
        response_model: type[T],
        *,
        tier_override: ModelTier | None = None,
        max_output_tokens: int | None = None,
        timeout_s: float | None = None,
        budget: CallBudget | None = None,
    ) -> ModelResponse[T]:
        raise TierUnavailable(self._tier, self._targets_tried)

    async def _empty(self) -> AsyncIterator[StreamEvent]:
        return
        yield StreamEvent()  # pragma: no cover - never reached; makes this an async generator

    def stream[T: BaseModel](
        self,
        role: str,
        messages: Sequence[Message],
        response_model: type[T],
        *,
        budget: CallBudget | None = None,
    ) -> AsyncIterator[StreamEvent]:
        return self._empty().__aiter__()  # pragma: no cover - rewrite only ever calls complete()

    async def capabilities(self, role: str) -> ModelCapabilities:
        raise NotImplementedError  # pragma: no cover


class ScriptedBackend:
    """One transport, offline: it answers with a canned object and records every turn.

    A real `ModelBackend` behind a real `LadderModelClient`, not a stubbed `ModelClient`, because
    the thing these tests exist to prove is that a rung's call *travels* — through the context,
    through routing, negotiation and validation, to something that could have been a network. A
    fake client would short-circuit exactly the stretch that used to be missing.
    """

    name: ClassVar[str] = "fake"
    version: ClassVar[int] = 1

    def __init__(self, value: BaseModel) -> None:
        self._value = value
        self.prompts: list[str] = []

    def declared_capabilities(self, target: BackendTarget) -> ModelCapabilities:
        return ModelCapabilities(supports_json_schema=True, max_output_tokens=8192)

    async def invoke(
        self,
        target: BackendTarget,
        messages: Sequence[Message],
        schema: dict[str, object] | None,
        mode: StructuredOutputMode,
        *,
        max_output_tokens: int,
        timeout_s: float,
    ) -> BackendReply:
        self.prompts.append("\n".join(message.content for message in messages))
        return BackendReply(
            text=self._value.model_dump_json(),
            usage=TokenUsage(input_tokens=200, output_tokens=40),
            finish_reason="stop",
        )


def ladder_client(backend: ScriptedBackend) -> LadderModelClient:
    """The REAL §7.7 client `RunContext` builds, over the shipped role table and one fake target.

    The price is real so §11.2's pre-dispatch gate has something to refuse; the router is real so
    `transform_repair` genuinely lands on WORKHORSE and `escalation` on HEAVY.
    """
    target = BackendTarget(
        backend="fake", model_id="fake-1", price=Price(in_per_mtok=1.0, out_per_mtok=2.0)
    )
    router = LlmRouter(
        dict(SPEC_ROLE_TIERS), dict.fromkeys(ModelTier, (target,)), profile="test"
    )
    return LadderModelClient(router, {"fake": backend})


# =======================================================================================
# helpers: a real repo, a real branch, a real commit log
# =======================================================================================
def git(repo: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],  # noqa: S607 - `git` from PATH, as every suite does
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def make_repo(tmp_path: Path, files: Mapping[str, str]) -> tuple[Path, str]:
    """A one-commit repo already on `migrate/<repo>`, and its phase anchor."""
    repo = tmp_path / "wt"
    repo.mkdir()
    git(repo, "init", f"--initial-branch={BRANCH}", ".")
    git(repo, "config", "user.email", "fleet@example.invalid")
    git(repo, "config", "user.name", "Fleet Test")
    for path, text in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    git(repo, "add", "--all")
    git(repo, "commit", "-m", "initial")
    return repo, git(repo, "rev-parse", "HEAD")


def log_entries(repo: Path, anchor: str) -> list[dict[str, str]]:
    """Every commit in `<anchor>..HEAD`, oldest first, with the trailers git itself parsed."""
    fmt = (
        "%H%x1f%(trailers:key=Fleet-Patch-Id,valueonly,separator=%x2c)"
        "%x1f%(trailers:key=Fleet-Task-Id,valueonly,separator=%x2c)"
        "%x1f%s%x1e"
    )
    raw = git(repo, "log", f"--format={fmt}", f"{anchor}..HEAD")
    entries: list[dict[str, str]] = []
    for record in raw.split("\x1e"):
        body = record.strip()
        if not body:
            continue
        sha, patch_id, task_id, subject = body.split("\x1f")
        entries.append(
            {
                "sha": sha,
                "patch_id": patch_id.strip(),
                "task_id": task_id.strip(),
                "subject": subject.strip(),
            }
        )
    return list(reversed(entries))


def make_ctx(
    workdir: Path,
    *,
    attempt: int = 1,
    tier: TransformTier = TransformTier.DETERMINISTIC,
    context_policy: ContextPolicy | None = None,
    llm: object | None = None,
    seconds_left: float = 3600.0,
    db: object | None = None,
) -> WorkerContext:
    """A context whose only real collaborators are the worktree and (optionally) a model client.

    `deadline` is an ABSOLUTE `loop.time()` and is honoured by every `git` subprocess, so it stays
    genuinely far in the future; the deadline TEST moves the worker's clock instead.
    """
    sentinel: Any = object()
    deadline = monotonic() + seconds_left
    return WorkerContext(
        run_id=RUN_ID,
        repo_id=REPO_ID,
        attempt=attempt,
        workdir=str(workdir),
        lease_owner=OWNER,
        lease_fence=1,
        deadline=deadline,
        cancel=asyncio.Event(),
        budget=CallBudget(remaining_tokens=200_000, remaining_usd=5.0, deadline=deadline),
        db=FakeDb() if db is None else db,
        llm=sentinel if llm is None else llm,
        router=sentinel,
        limits=sentinel,
        log=sentinel,
        tier=tier,
        context_policy=context_policy,
    )


def rule(rule_id: str, *, priority: int = 100) -> RewriteRule:
    return RewriteRule(
        id=rule_id,
        engine="fake",
        languages=["python"],
        applies_to=["**/*.py"],
        rule={"pattern": "unused-by-the-fake"},
        priority=priority,
    )


def worker_with(
    engine: FakeRewriter, *, max_passes: int = 3
) -> RewriteWorker:
    """The REAL `RewriteWorker`, with only its pipeline factory pointed at an in-process engine.

    `run()`, `preconditions_hold()` and the whole commit sequence under test are the shipped ones;
    `pipeline_for` exists as a seam precisely because the three real engines are external tools.
    """

    class _Injected(RewriteWorker):
        __slots__ = ()

        def pipeline_for(self, ctx: WorkerContext, payload: RewriteInput) -> RewritePipeline:
            return RewritePipeline(
                payload.rules,
                EngineRegistry([engine]),
                max_passes=max_passes,
                params=payload.params,
                tier=ctx.tier,
                repo_id=ctx.repo_id,
            )

    return _Injected()


def rewrite_payload(anchor: str, targets: Sequence[str], **kwargs: Any) -> RewriteInput:
    kwargs.setdefault("rules", [rule("r1")])
    return RewriteInput(
        branch=BRANCH,
        phase_pre_commit_sha=anchor,
        dest_path=DEST,
        targets=list(targets),
        **kwargs,
    )


def clock_expiring_after(deadline: float, units: int) -> Callable[[], float]:
    """A monotonic stand-in that crosses `deadline` after exactly `units` unit-boundary checks.

    `run()` reads the clock once per unit, so this is how "the wall clock ran out mid-run" becomes
    a deterministic fact instead of a race against a real timer.
    """
    seen = {"calls": 0}

    def fake_now() -> float:
        seen["calls"] += 1
        return deadline - 1.0 if seen["calls"] <= units else deadline + 1.0

    return fake_now


# =======================================================================================
# 1. both workers are real: they claim their preconditions and can be constructed
# =======================================================================================
def test_both_transform_workers_are_concrete_and_claim_their_own_preconditions() -> None:
    """A worker that inherits `preconditions_hold` is un-instantiable by construction (§7.1).

    Why it matters: the default `return True` this replaced admitted every re-entry, which is the
    blind replay the method exists to forbid — `relocate` re-running a rename produces
    `java/java/com/x`. Both classes must therefore OVERRIDE it and be constructible.
    """
    for cls in (RewriteWorker, RelocateWorker):
        assert implements_preconditions(cls)
        assert cls.preconditions_hold is not BaseWorker.preconditions_hold
        worker = cls()
        assert worker.phase is Phase.TRANSFORM
        assert_stateless(worker)  # a registry singleton may not carry instance state (§7.2)


# =======================================================================================
# 2. THE headline: 40 of 60 land, the deadline hits, re-entry replays none of them
# =======================================================================================
def test_forty_of_sixty_land_then_the_deadline_makes_it_partial_and_re_entry_replays_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interrupted work is `partial`, and the 40 commits it left are never written twice.

    Why it matters: a boolean verdict here is a lie about the tree. Reporting `failed` with no
    output makes the next attempt replay all 60 against an already-rewritten tree and emit 40
    no-op patches the ladder misreads as `RULE_MISS`. The proof is the commit log — the returned
    lists are a claim, the log is the fact — so the first 40 SHAs must be byte-identical before
    and after re-entry, and the branch must end with exactly 60 commits, not 100.
    """
    units = [f"{DEST}/f{i:02d}.py" for i in range(60)]
    repo, anchor = make_repo(tmp_path, dict.fromkeys(units, "alpha\nbeta\n"))
    worker = worker_with(FakeRewriter({"r1": lambda t: t.replace("beta", "BETA")}))

    ctx = make_ctx(repo)
    monkeypatch.setattr(rewrite_mod, "loop_now", clock_expiring_after(ctx.deadline, 40))
    execution = asyncio.run(worker.execute(ctx, rewrite_payload(anchor, units), max_attempts=3))
    monkeypatch.undo()

    first = execution.final
    assert first is not None
    assert first.status == "partial", "landed work reported as failure is the whole defect"
    assert len(first.completed_units) == 40
    assert first.remaining_units == units[40:]
    assert execution.status is RepoStatus.PENDING, "a partial repo is re-queued, not terminal"

    landed_first = log_entries(repo, anchor)
    assert len(landed_first) == 40, "exactly the units it claimed, no more"

    # -- re-entry: the runner replays the checkpoint onto the payload and nothing else ---
    resumed = rewrite_payload(anchor, units, completed_units=first.completed_units)
    ctx2 = make_ctx(repo)
    assert asyncio.run(worker.preconditions_hold(ctx2, resumed)) is True
    second = asyncio.run(worker.run(ctx2, resumed))

    assert second.status == "ok"
    assert len(second.completed_units) == 60, "the checkpoint carries forward, cumulatively"
    landed_all = log_entries(repo, anchor)
    assert len(landed_all) == 60, "re-entry added 20 commits, not another 60"
    assert [e["sha"] for e in landed_all[:40]] == [e["sha"] for e in landed_first], (
        "the first 40 commits were not rewritten, re-created, or duplicated"
    )
    assert (repo / units[59]).read_text(encoding="utf-8") == "alpha\nBETA\n"


# =======================================================================================
# 3. the guard is TWO conditions, and the trailer alone never decides
# =======================================================================================
def test_the_guard_skips_a_patch_present_at_the_tip(tmp_path: Path) -> None:
    """Both halves true → skip, no second commit, no `already_applied` work.

    Why it matters: re-running a rung after a crash must be free. The trailer supplies the commit
    SHA for the `attempts` row; the reverse-apply proves the effect is really in the tree.
    """
    unit = f"{DEST}/mod.py"
    repo, anchor = make_repo(tmp_path, {unit: "alpha\nbeta\n"})
    worker = worker_with(FakeRewriter({"r1": lambda t: t.replace("beta", "BETA")}))
    payload = rewrite_payload(anchor, [unit])

    first = asyncio.run(worker.run(make_ctx(repo), payload))
    assert first.status == "ok"
    assert len(log_entries(repo, anchor)) == 1
    assert first.output is not None and first.output.rewritten == [unit], (
        "D49 leg 3, deterministic branch: `unit` genuinely IS the landed path here, so `_record` "
        "must still report it — this is the no-regression case for the fix below"
    )

    second = asyncio.run(worker.run(make_ctx(repo), payload))
    assert second.status == "ok"
    assert second.output is not None and second.output.skipped == [unit]
    assert len(log_entries(repo, anchor)) == 1, "a present patch is skipped, never re-committed"


def test_a_patch_whose_effect_a_revert_dropped_is_re_applied_despite_its_trailer(
    tmp_path: Path,
) -> None:
    """Trailer present, effect gone → RE-APPLY. This is the defect the second condition exists for.

    Why it matters: a trailer-only guard permanently skipped a patch whose hunk a later rebase had
    dropped, and the consumer shipped a PR pointing at a path that no longer existed. The trailer
    proves the patch was once committed; only `git apply --check --reverse` proves it survived.
    """
    unit = f"{DEST}/mod.py"
    repo, anchor = make_repo(tmp_path, {unit: "alpha\nbeta\n"})
    worker = worker_with(FakeRewriter({"r1": lambda t: t.replace("beta", "BETA")}))
    payload = rewrite_payload(anchor, [unit])

    asyncio.run(worker.run(make_ctx(repo), payload))
    landed = log_entries(repo, anchor)
    patch_id = landed[0]["patch_id"]

    # a rebase/revert drops the effect while the trailered commit stays in the scoped range
    git(repo, "revert", "--no-edit", "--no-commit", landed[0]["sha"])
    git(repo, "commit", "-m", "a later rebase dropped the hunk")
    assert (repo / unit).read_text(encoding="utf-8") == "alpha\nbeta\n"

    again = asyncio.run(worker.run(make_ctx(repo), payload))

    assert again.status == "ok"
    assert again.output is not None and again.output.skipped == [], "the trailer must not skip it"
    assert (repo / unit).read_text(encoding="utf-8") == "alpha\nBETA\n", "the effect is restored"
    carrying = [e for e in log_entries(repo, anchor) if e["patch_id"] == patch_id]
    assert len(carrying) == 2, "the same content-addressed patch id, committed twice, on purpose"


# =======================================================================================
# 4. relocate: never a doubled path
# =======================================================================================
def test_relocate_run_twice_moves_the_tree_once_and_never_doubles_the_path(
    tmp_path: Path,
) -> None:
    """`java/java/com/x` is what an inherited `preconditions_hold` produced (§7.1).

    Why it matters: the second invocation must be a *skip*, proven by the guard's reverse-apply on
    the rename, not by a bespoke "have I moved this already?" flag that can disagree with the tree.
    """
    source = "com/x/A.java"
    repo, anchor = make_repo(tmp_path, {source: "class A {}\n"})
    worker = RelocateWorker()
    payload = RelocateInput(
        branch=BRANCH, phase_pre_commit_sha=anchor, dest_path="java", sources=[source]
    )

    first = asyncio.run(worker.run(make_ctx(repo), payload))
    assert first.status == "ok"
    assert (repo / "java/com/x/A.java").is_file()
    assert not (repo / source).exists()
    assert first.output is not None and first.output.moved == {source: "java/com/x/A.java"}

    second = asyncio.run(worker.run(make_ctx(repo), payload))

    assert second.status == "ok"
    assert second.output is not None and second.output.skipped == [source]
    assert not (repo / "java/java").exists(), "the doubled path this worker exists to prevent"
    assert len(log_entries(repo, anchor)) == 1, "one move, one commit, however often it re-runs"


def test_relocate_refuses_a_plan_computed_against_an_already_relocated_tree(
    tmp_path: Path,
) -> None:
    """A plan whose sources already sit under `dest_path` does not describe this tree.

    Why it matters: this is the `java/java/com/x` case at its origin — a resumed run recomputing
    the plan from the *current* tree. `preconditions_hold` returning False re-runs the phase from
    `phases.base_ref` instead of moving the tree a second time.
    """
    repo, anchor = make_repo(tmp_path, {"java/com/x/A.java": "class A {}\n"})
    worker = RelocateWorker()
    already = RelocateInput(
        branch=BRANCH,
        phase_pre_commit_sha=anchor,
        dest_path="java",
        sources=["java/com/x/A.java"],
    )
    honest = RelocateInput(
        branch=BRANCH, phase_pre_commit_sha=anchor, dest_path="ts", sources=["java/com/x/A.java"]
    )

    assert asyncio.run(worker.preconditions_hold(make_ctx(repo), already)) is False
    assert asyncio.run(worker.preconditions_hold(make_ctx(repo), honest)) is True
    assert relocated_path("java/", "com/x/A.java") == "java/com/x/A.java"


def test_relocate_precondition_admits_a_unit_whose_move_already_landed(tmp_path: Path) -> None:
    """A checkpoint-less re-entry over a half-moved tree is admitted, not rejected.

    Why it matters: the crash window between `git commit` and the row write is normal (§3.2 step
    6.4). A source that is gone but present at its destination is *landed work*, and the guard
    will skip it; failing the precondition there would re-run the whole phase for nothing.
    """
    repo, anchor = make_repo(tmp_path, {"java/com/x/A.java": "class A {}\n", "b.txt": "b\n"})
    payload = RelocateInput(
        branch=BRANCH,
        phase_pre_commit_sha=anchor,
        dest_path="java",
        sources=["com/x/A.java", "b.txt"],
    )
    assert asyncio.run(RelocateWorker().preconditions_hold(make_ctx(repo), payload)) is True

    missing = payload.model_copy(update={"sources": ["com/x/A.java", "gone.txt"]})
    assert asyncio.run(RelocateWorker().preconditions_hold(make_ctx(repo), missing)) is False


# =======================================================================================
# 5. a RuleConflict is an operator's defect: file unchanged, ladder unmoved
# =======================================================================================
def test_a_rule_conflict_leaves_the_file_unchanged_and_spends_no_rung(tmp_path: Path) -> None:
    """Two rules claiming one span is a YAML defect, and no LLM rung can repair YAML.

    Why it matters: charging it to the ladder buys a `WORKHORSE` and then a `HEAVY` call to
    rediscover that two rules disagree. `RetryPolicy.decide` must answer TERMINATE with
    `charges_attempt == False`, which is the executable form of the pipeline's
    `advance_ladder=False`.
    """
    unit = f"{DEST}/mod.py"
    repo, anchor = make_repo(tmp_path, {unit: "alpha\nbeta\ngamma\n"})
    engine = FakeRewriter(
        {"r1": lambda t: t.replace("beta", "MIDDLE"), "r2": lambda t: t.replace("MIDDLE", "FINAL")}
    )
    worker = worker_with(engine)
    payload = rewrite_payload(anchor, [unit])
    payload.rules = [rule("r1"), rule("r2", priority=200)]

    result = asyncio.run(worker.run(make_ctx(repo), payload))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.retryable is False, "a rule defect must not advance the ladder"
    assert "RuleConflict" in result.error.stderr_tail
    assert (repo / unit).read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n", "file unchanged"
    assert log_entries(repo, anchor) == [], "nothing was committed"

    decision = RetryPolicy().decide(LadderState(attempts=0, max_attempts=3), result.error)
    assert decision.action is RetryAction.TERMINATE
    assert decision.charges_attempt is False
    assert decision.state.attempts == 0, "no rung was spent on an operator's YAML"


# =======================================================================================
# 5a. security finding #7 (CRITICAL) — a tracked symlink target is refused, never dereferenced
# =======================================================================================
def test_a_tracked_symlink_target_is_refused_before_it_is_dereferenced(tmp_path: Path) -> None:
    """A source repo can commit a symlink at a path a `RewriteRule` targets. `Path.read_text()`
    follows a symlink exactly like the OS does, so without a guard the target's content becomes
    `"current_content"` in `_evidence()` and is shipped, unredacted, to a third-party LLM — a
    complete arbitrary-host-file-read-and-egress primitive requiring no LLM compromise at all.

    `(root / unit).is_symlink()` must refuse loud, before the read, with `UnsafeSourcePathError` —
    and never merely skip the unit, which would look identical to "nothing to do here" and lose
    the loud-failure property Rule 11 requires.

    DISCRIMINATES: reverting the `is_symlink()` guard in `rewrite.py`'s per-unit loop makes this
    test fail — `pytest.raises` sees no exception, because the old code reads the sentinel content
    straight through the symlink and hands it to the (fake) engine instead of refusing.
    """
    unit = f"{DEST}/mod.py"
    repo, _initial_anchor = make_repo(tmp_path, {f"{DEST}/other.py": "keep\n"})
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("SENTINEL_SECRET_CONTENT\n", encoding="utf-8")
    link_path = repo / unit
    link_path.parent.mkdir(parents=True, exist_ok=True)
    link_path.symlink_to(outside)
    git(repo, "add", "--all")
    git(repo, "commit", "-m", "attacker-controlled: track a symlink at the rewrite target")
    anchor = git(repo, "rev-parse", "HEAD")  # phase anchor already carries the symlink

    engine = FakeRewriter({"r1": lambda t: t})
    worker = worker_with(engine)
    payload = rewrite_payload(anchor, [unit])

    with pytest.raises(UnsafeSourcePathError, match=unit):
        asyncio.run(worker.run(make_ctx(repo), payload))

    assert engine.seen == [], "the sentinel content never reached the pipeline/engine at all"
    assert log_entries(repo, anchor) == [], "a refused unit is never committed"


def test_targets_are_present_treats_a_symlinked_unit_as_absent(tmp_path: Path) -> None:
    """`_targets_are_present` (`preconditions_hold`'s check) used `.is_file()`, which — like
    `.read_text()` — follows a symlink, so a symlinked target read as "present" and admitted
    re-entry as if the plan still described an ordinary file. A symlinked unit must read as
    NOT present, the same verdict as a vanished one, so `preconditions_hold` returns False and
    the phase re-runs from `phases.base_ref` rather than treating the symlink as routine.
    """
    unit = f"{DEST}/mod.py"
    repo, anchor = make_repo(tmp_path, {f"{DEST}/other.py": "keep\n"})
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("SENTINEL_SECRET_CONTENT\n", encoding="utf-8")
    link_path = repo / unit
    link_path.parent.mkdir(parents=True, exist_ok=True)
    link_path.symlink_to(outside)
    git(repo, "add", "--all")
    git(repo, "commit", "-m", "attacker-controlled: track a symlink at the rewrite target")

    worker = RewriteWorker()
    payload = rewrite_payload(anchor, [unit])

    assert asyncio.run(worker.preconditions_hold(make_ctx(repo), payload)) is False


# =======================================================================================
# 5b. D49 — the dead patch cap enforced, and the LLM branch gated the same way
# =======================================================================================
def test_an_oversize_deterministic_patch_is_rejected_before_it_is_ever_committed(
    tmp_path: Path,
) -> None:
    """`transform.max_patch_bytes` was declared, enforced inside `check_diff`, and connected to
    nothing: the one production call site omitted `max_bytes` (D49 leg 1), so the ceiling §11.3
    and `SPEC.md:6059,6634,7056` all describe was unreachable code — no diff could ever be too
    big. `RewriteInput.max_patch_bytes` closes that: the deterministic call site now threads it,
    and the error names the setting so an operator reading `stderr_tail` knows which knob to
    turn, not just that something was rejected.
    """
    unit = f"{DEST}/mod.py"
    repo, anchor = make_repo(tmp_path, {unit: "alpha\n"})
    worker = worker_with(FakeRewriter({"r1": lambda t: t.replace("alpha", "ALPHA" * 200)}))
    payload = rewrite_payload(anchor, [unit], max_patch_bytes=64)

    result = asyncio.run(worker.run(make_ctx(repo), payload))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.failure_class is FailureClass.PATCH_REJECTED
    assert "transform.max_patch_bytes" in result.error.stderr_tail, (
        "the message must name the setting an operator would need to raise"
    )
    assert (repo / unit).read_text(encoding="utf-8") == "alpha\n", (
        "the oversize patch never applied"
    )
    assert log_entries(repo, anchor) == [], "nothing was committed"


def test_a_model_patch_outside_dest_path_is_rejected_before_it_is_ever_landed(
    tmp_path: Path,
) -> None:
    """D49 leg 3: the LLM branch called `land_patches` with no diff check and no parse probe at
    all — `apply_and_commit` runs an idempotency check and `git apply --check`, never
    `check_diff`, and `parse_probe_ok=False` is hard-coded because the model may not certify its
    own patch (`llm/schemas.py`). Nothing else stood between a model-authored patch that names a
    path outside the repo's own monorepo subtree and a commit. This pins that the same
    `check_diff` gate the deterministic branch already had now runs on `repair.patches` too,
    BEFORE `land_patches`, so the escape is refused rather than merely audited after the fact.
    """
    unit = f"{DEST}/mod.py"
    repo, anchor = make_repo(tmp_path, {unit: "alpha\n"})
    worker = worker_with(FakeRewriter({}))  # no rule fires: RULE_MISS, the rung-2 case
    client = FakeModelClient(
        _proposal("elsewhere/other.py", "old\n", "new\n", marker="escape")
    )
    ctx = make_ctx(
        repo,
        attempt=2,
        tier=TransformTier.LLM_REPAIR,
        context_policy=ContextPolicy.EVIDENCE_ONLY,
        llm=client,
    )

    result = asyncio.run(worker.run(ctx, rewrite_payload(anchor, [unit])))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.failure_class is FailureClass.PATCH_REJECTED
    assert result.error.retryable is True, "a later rung may still propose an in-tree patch"
    assert "elsewhere/other.py" in result.error.stderr_tail
    assert "escape the repo subtree" in result.error.stderr_tail
    assert (repo / unit).read_text(encoding="utf-8") == "alpha\n", "the target file is untouched"
    assert log_entries(repo, anchor) == [], "the out-of-subtree patch was never committed"


# =======================================================================================
# 5c. `check_diff`'s `declared_path` gate is actually wired into both call sites
# =======================================================================================
def test_a_model_patch_whose_declared_path_disagrees_with_its_diff_is_rejected_before_landing(
    tmp_path: Path,
) -> None:
    """`ProposedFileEdit.path` and `.diff` are two independently model-supplied fields
    (`llm/schemas.py`); nothing upstream of `_as_patches` forces them to name the same file.
    `git apply` only ever looks at the diff's own `---`/`+++` headers, so a mismatch means the
    file the diff actually writes and the file `FilePatch.path` claims was written are different —
    and `cli._transform_criterion`'s §3.2 parse probe reads exactly `output.rewritten`
    (`patch.path`), so the file really on disk would ship unprobed while an unrelated path is
    certified clean. Both paths stay inside `dest_path` here, so this fails ONLY on the
    declared/diff mismatch, not on the (already-covered) subtree escape.
    """
    unit = f"{DEST}/mod.py"
    repo, anchor = make_repo(tmp_path, {unit: "alpha\n"})
    worker = worker_with(FakeRewriter({}))  # no rule fires: RULE_MISS, the rung-2 case
    decoy = f"{DEST}/decoy.py"
    diff = make_unified_diff(unit, "alpha\n", "beta\n")
    client = FakeModelClient(
        LlmPatchProposal(
            files=(ProposedFileEdit(path=decoy, diff=diff),),
            approach_summary="rewrite mod.py",
            rationale="the deterministic rule could not land",
        )
    )
    ctx = make_ctx(
        repo,
        attempt=2,
        tier=TransformTier.LLM_REPAIR,
        context_policy=ContextPolicy.EVIDENCE_ONLY,
        llm=client,
    )

    result = asyncio.run(worker.run(ctx, rewrite_payload(anchor, [unit])))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.failure_class is FailureClass.PATCH_REJECTED
    assert result.error.retryable is True, "a later rung may still propose a self-consistent patch"
    assert decoy in result.error.stderr_tail
    assert unit in result.error.stderr_tail
    assert (repo / unit).read_text(encoding="utf-8") == "alpha\n", (
        "the diff's real target is untouched"
    )
    assert log_entries(repo, anchor) == [], "the mismatched patch was never committed"


def test_a_model_patch_whose_declared_path_agrees_with_its_diff_still_lands(
    tmp_path: Path,
) -> None:
    """The regression pin for the case above: a self-consistent `ProposedFileEdit` — declared
    `path` equal to the one path its own diff writes — must not be caught by the new gate."""
    unit = f"{DEST}/mod.py"
    repo, anchor = make_repo(tmp_path, {unit: "alpha\n"})
    worker = worker_with(FakeRewriter({}))  # no rule fires: RULE_MISS, the rung-2 case
    client = FakeModelClient(_proposal(unit, "alpha\n", "beta\n", marker="repair"))
    ctx = make_ctx(
        repo,
        attempt=2,
        tier=TransformTier.LLM_REPAIR,
        context_policy=ContextPolicy.EVIDENCE_ONLY,
        llm=client,
    )

    result = asyncio.run(worker.run(ctx, rewrite_payload(anchor, [unit])))

    assert result.status == "ok", result.error
    assert (repo / unit).read_text(encoding="utf-8") == "beta\n"
    assert result.output is not None and result.output.rewritten == [unit]


def test_a_model_patch_declaring_a_renames_destination_path_is_still_accepted(
    tmp_path: Path,
) -> None:
    """`diff_paths` resolves a rename to its POST-image path only (`794ee24`), so a model that
    declares the destination of its own rename must be accepted — declaring the stale source
    would be the actual bug. This is the shape most likely to break under a naive
    `declared_path == diff's-only-path` check, exercised here through the real `RewriteWorker`
    and a real `git apply --index`, not just `check_diff` in isolation.
    """
    old_path = f"{DEST}/old.py"
    new_path = f"{DEST}/new.py"
    before = "alpha\nbeta\ngamma\ndelta\n"
    repo, anchor = make_repo(tmp_path, {old_path: before})
    worker = worker_with(FakeRewriter({}))  # no rule fires: RULE_MISS, the rung-2 case
    rename_and_edit_diff = (
        f"diff --git a/{old_path} b/{new_path}\n"
        "similarity index 75%\n"
        f"rename from {old_path}\n"
        f"rename to {new_path}\n"
        f"--- a/{old_path}\n"
        f"+++ b/{new_path}\n"
        "@@ -1,4 +1,4 @@\n"
        "-alpha\n"
        "+ALPHA\n"
        " beta\n"
        " gamma\n"
        " delta\n"
    )
    client = FakeModelClient(
        LlmPatchProposal(
            files=(ProposedFileEdit(path=new_path, diff=rename_and_edit_diff),),
            approach_summary="rename old.py to new.py and fix the header",
            rationale="the deterministic rule cannot rename; the model can",
        )
    )
    ctx = make_ctx(
        repo,
        attempt=2,
        tier=TransformTier.LLM_REPAIR,
        context_policy=ContextPolicy.EVIDENCE_ONLY,
        llm=client,
    )

    result = asyncio.run(worker.run(ctx, rewrite_payload(anchor, [old_path])))

    assert result.status == "ok", result.error
    assert not (repo / old_path).exists(), "the source path is gone"
    assert (repo / new_path).read_text(encoding="utf-8") == "ALPHA\nbeta\ngamma\ndelta\n"
    assert result.output is not None and result.output.rewritten == [new_path], (
        "the declared DESTINATION path landed and was recorded — never the stale source"
    )


# =======================================================================================
# 6. the repair rung is shown this failure, verbatim, and nothing else
# =======================================================================================
def _proposal(path: str, before: str, after: str, *, marker: str) -> LlmPatchProposal:
    return LlmPatchProposal(
        files=(ProposedFileEdit(path=path, diff=make_unified_diff(path, before, after)),),
        approach_summary=f"rewrite {path} ({marker})",
        rationale="the deterministic rule could not land; patch the clean sibling instead",
    )


def test_the_repair_prompt_carries_this_failures_verbatim_stderr_and_no_prior_transcript(
    tmp_path: Path,
) -> None:
    """Evidence, verbatim and fresh; priors, never (guardrail 5, ADR-0021).

    Why it matters: a repair loop shown an accumulated transcript anchors on the approach that
    already failed and spends rung 3 producing rung 2 with different whitespace. So the prompt
    must contain the stderr of the failure it is repairing RIGHT NOW, and must not contain the
    previous invocation's failure text, the rejected diff, or — under `EVIDENCE_ONLY` — the
    rejected-approach summaries a higher policy would carry.
    """
    one, two, clean = f"{DEST}/one.py", f"{DEST}/two.py", f"{DEST}/clean.py"
    repo, anchor = make_repo(
        tmp_path, {one: "alpha\n", two: "alpha\n", clean: "keep\n"}
    )
    marker = "ANCHOR_ME_PRIOR_DIFF"
    engine = FakeRewriter({"r1": lambda t: t.replace("alpha", f"alpha {marker}")})
    worker = worker_with(engine)

    # A worktree that does not match the index is a patch `git apply --index` refuses, verbatim.
    (repo / one).write_text("alpha dirty\n", encoding="utf-8")
    attempt_one = asyncio.run(worker.run(make_ctx(repo), rewrite_payload(anchor, [one])))
    assert attempt_one.status == "failed" and attempt_one.error is not None
    assert "one.py" in attempt_one.error.stderr_tail

    # The task-anchored discard has already reverted the tree, so attempt 2 dirties its own file.
    (repo / two).write_text("alpha dirty\n", encoding="utf-8")

    client = FakeModelClient(_proposal(clean, "keep\n", "kept\n", marker="repair"))
    ctx = make_ctx(
        repo,
        attempt=2,
        tier=TransformTier.LLM_REPAIR,
        context_policy=ContextPolicy.EVIDENCE_ONLY,
        llm=client,
    )
    payload = rewrite_payload(
        anchor,
        [two],
        rejected_approaches=[
            RejectedApproach(
                approach_signature="b" * 64,
                reason="REJECTED_APPROACH_SUMMARY_MARKER",
                failure_class=FailureClass.PATCH_REJECTED,
                attempt=1,
                tier=TransformTier.DETERMINISTIC,
            )
        ],
    )

    result = asyncio.run(worker.run(ctx, payload))

    assert client.roles == [str(Role.TRANSFORM_REPAIR)], "attempt 2 is the WORKHORSE rung"
    prompt = client.prompts[0]
    assert "does not match index" in prompt, "the verbatim git refusal, not a paraphrase"
    assert "two.py" in prompt, "the failure being repaired right now"
    assert "one.py" not in prompt, "the PREVIOUS invocation's failure is not carried forward"
    assert marker not in prompt, "the rejected diff is never rendered (the anchoring hazard)"
    assert "REJECTED_APPROACH_SUMMARY_MARKER" not in prompt, "EVIDENCE_ONLY carries no priors"
    assert "alpha" in prompt, "the target file's current content IS evidence"

    assert result.status == "ok", "the repair patch landed"
    assert (repo / clean).read_text(encoding="utf-8") == "kept\n"
    assert result.usage.cost_usd == pytest.approx(0.01), "the rung's spend is reported"
    assert result.output is not None and result.output.rewritten == [clean], (
        "D49 leg 3: the model repaired `two` by patching `clean` instead — `_record` must report "
        "the landed `clean`, never the deterministic unit name `two`, which the model never wrote"
    )


def test_the_repair_prompt_omits_every_line_of_a_rejected_diff_and_the_prior_failure_class(
    tmp_path: Path,
) -> None:
    """§12.35's two worker-level gaps beyond the marker test above (round AA task 3).

    1. The test above never asserts a prior `RejectedApproach.failure_class` token is absent —
       only its `reason` summary (`REJECTED_APPROACH_SUMMARY_MARKER`). `failure_class` travels
       through a SEPARATE rendering branch (`rewrite.py`'s `_evidence`, the `prior.failure_class`
       leg vs. the `prior.reason` leg), so a leak there would pass the test above undetected.
    2. The test above checks diff-absence via one anchor string (`ANCHOR_ME_PRIOR_DIFF`). A bug
       that filtered out that exact marker while still leaking OTHER lines of the same rejected
       diff would pass that test. Here every line of a real multi-line diff is captured with
       `make_unified_diff` — the same call the production code makes — and swept individually.
    """
    one, two, clean = f"{DEST}/one.py", f"{DEST}/two.py", f"{DEST}/clean.py"
    committed = "L1\nL2\nL3\nL4\n"
    dirty = "L1 DIRTY\nL2\nL3\nL4\n"
    after = (
        "REJECTED_LINE_1_UNIQUE\nREJECTED_LINE_2_UNIQUE\n"
        "REJECTED_LINE_3_UNIQUE\nREJECTED_LINE_4_UNIQUE\n"
    )
    repo, anchor = make_repo(tmp_path, {one: committed, two: committed, clean: "keep\n"})

    # The literal diff the production rule/engine would build for this rejected patch, captured
    # with the SAME helper (`make_unified_diff`) the pipeline calls internally — a real multi-line
    # diff, not a hand-typed guess at git's unified-diff format.
    rejected_diff = make_unified_diff(one, dirty, after)
    assert rejected_diff, "fixture sanity: the rule actually changes the file"
    rejected_lines = [line for line in rejected_diff.splitlines() if line.strip()]
    assert len(rejected_lines) >= 8, "fixture sanity: a genuine multi-line diff, not one line"

    # `r1` always rewrites to `after` regardless of the file it's given, so BOTH `one.py`'s
    # attempt (a separate, earlier `worker.run()`) and `two.py`'s own deterministic pre-repair
    # pass (inside THIS `worker.run()`) produce and then fail to apply the SAME rejected diff —
    # the strongest form of the property: neither a genuinely prior invocation's diff nor the
    # CURRENT rung's own just-rejected diff may reach the repair prompt.
    engine = FakeRewriter({"r1": lambda _source: after})
    worker = worker_with(engine)

    # A worktree that does not match the index is a patch `git apply --index` refuses, verbatim.
    (repo / one).write_text(dirty, encoding="utf-8")
    attempt_one = asyncio.run(worker.run(make_ctx(repo), rewrite_payload(anchor, [one])))
    assert attempt_one.status == "failed" and attempt_one.error is not None

    (repo / two).write_text(dirty, encoding="utf-8")

    client = FakeModelClient(_proposal(clean, "keep\n", "kept\n", marker="repair"))
    ctx = make_ctx(
        repo,
        attempt=2,
        tier=TransformTier.LLM_REPAIR,
        context_policy=ContextPolicy.EVIDENCE_ONLY,
        llm=client,
    )
    payload = rewrite_payload(
        anchor,
        [two],
        rejected_approaches=[
            RejectedApproach(
                approach_signature="c" * 64,
                reason="an unrelated approach; no diff text lives here (schema forbids it)",
                failure_class=FailureClass.BUDGET_EXHAUSTED,
                attempt=1,
                tier=TransformTier.DETERMINISTIC,
            )
        ],
    )

    result = asyncio.run(worker.run(ctx, payload))

    assert client.roles == [str(Role.TRANSFORM_REPAIR)], "attempt 2 is the WORKHORSE rung"
    prompt = client.prompts[0]

    # gap 1: the PRIOR RejectedApproach's `failure_class` token never leaks. `FailureClass` is a
    # `StrEnum` whose `.name`/`.value`/`str()` all coincide (`PATCH_REJECTED` etc, checked against
    # `src/fleet/models/enums.py`), and `rewrite.py::_evidence` renders priors with `str(prior.
    # failure_class)` (line ~563) — so `str()` is the discriminating form to assert against, the
    # one the production code would actually emit if this leaked.
    assert str(FailureClass.BUDGET_EXHAUSTED) not in prompt, (
        "a prior RejectedApproach's failure_class token leaked into the repair prompt"
    )
    # BUDGET_EXHAUSTED is distinctive from this attempt's OWN current failure_class
    # (PATCH_REJECTED, from the git-apply refusal above), which the prompt legitimately DOES show
    # as evidence of the failure being repaired right now — so this is not a duplicate check.
    assert result.error is None or "BUDGET_EXHAUSTED" not in (result.error.stderr_tail or "")

    # gap 2: EVERY line of a real multi-line rejected diff is absent — not just one marker.
    for line in rejected_lines:
        assert line not in prompt, f"a rejected diff line leaked into the prompt: {line!r}"

    assert result.status == "ok", "the repair patch still landed despite the rejected diffs"
    assert (repo / clean).read_text(encoding="utf-8") == "kept\n"


def test_the_repair_prompt_shows_every_line_of_a_prior_rejected_diff_under_evidence_plus_priors(
    tmp_path: Path,
) -> None:
    """§12.35 positive control — SPEC.md:7461's own proof shape, inverted (round GG task 4).

    The test above proves a raw prior diff is ABSENT from the repair prompt under
    `EVIDENCE_ONLY`/`EVIDENCE_PLUS_REJECTED_APPROACHES`. This proves the literal inverse: once the
    policy is `EVIDENCE_PLUS_PRIORS`, every non-blank line of a genuinely prior rejected diff DOES
    appear — carried via `RewriteInput.prior_rejected_diffs`, never via `RejectedApproach` (which
    still has no field able to hold one; `RejectedApproach.model_fields` is untouched by this
    design — 5 fields, none diff-shaped) and never persisted to SQLite or git.
    """
    one, two, clean = f"{DEST}/one.py", f"{DEST}/two.py", f"{DEST}/clean.py"
    committed = "L1\nL2\nL3\nL4\n"
    dirty = "L1 DIRTY\nL2\nL3\nL4\n"
    after = (
        "REJECTED_LINE_1_UNIQUE\nREJECTED_LINE_2_UNIQUE\n"
        "REJECTED_LINE_3_UNIQUE\nREJECTED_LINE_4_UNIQUE\n"
    )
    repo, anchor = make_repo(tmp_path, {one: committed, two: committed, clean: "keep\n"})

    # The same real, multi-line diff the negative-control test above captures — built with the
    # SAME helper (`make_unified_diff`) production code uses for a genuinely rejected patch.
    rejected_diff = make_unified_diff(one, dirty, after)
    assert rejected_diff, "fixture sanity: the rule actually changes the file"
    rejected_lines = [line for line in rejected_diff.splitlines() if line.strip()]
    assert len(rejected_lines) >= 8, "fixture sanity: a genuine multi-line diff, not one line"

    engine = FakeRewriter({"r1": lambda _source: after})
    worker = worker_with(engine)

    # Attempt 2: a dirty worktree makes `git apply --index` refuse, verbatim — the same trick the
    # negative-control test uses to actually produce a rejected `FilePatch`-shaped diff.
    (repo / one).write_text(dirty, encoding="utf-8")
    attempt_two = asyncio.run(
        worker.run(make_ctx(repo, attempt=2), rewrite_payload(anchor, [one]))
    )
    assert attempt_two.status == "failed" and attempt_two.error is not None

    # Attempt 3: force a repair rung on a DIFFERENT unit (two.py, same dirty-worktree trick),
    # under EVIDENCE_PLUS_PRIORS, with attempt 2's captured diff threaded via
    # `prior_rejected_diffs` — this is the in-process carrier the ladder driver (a test today,
    # `PhaseRunner._drive()` later) is responsible for threading; the worker itself is stateless.
    (repo / two).write_text(dirty, encoding="utf-8")
    client = FakeModelClient(_proposal(clean, "keep\n", "kept\n", marker="repair"))
    ctx = make_ctx(
        repo,
        attempt=3,
        tier=TransformTier.LLM_REPAIR,
        context_policy=ContextPolicy.EVIDENCE_PLUS_PRIORS,
        llm=client,
    )
    payload = rewrite_payload(
        anchor,
        [two],
        prior_rejected_diffs=[
            FilePatch(
                path=one,
                diff=rejected_diff,
                tier=TransformTier.DETERMINISTIC,
                parse_probe_ok=False,
            )
        ],
    )

    result = asyncio.run(worker.run(ctx, payload))

    assert client.roles == [str(Role.TRANSFORM_REPAIR)], "attempt 3 is the WORKHORSE rung"
    prompt = client.prompts[0]

    # The positive control: EVERY non-blank line of the prior rejected diff appears in the
    # attempt-3 prompt — the literal inverse of the negative-control test's assertion above.
    for line in rejected_lines:
        assert line in prompt, f"a prior rejected diff line failed to appear: {line!r}"

    assert result.status == "ok", "the repair patch still landed"
    assert (repo / clean).read_text(encoding="utf-8") == "kept\n"


def test_a_populated_prior_rejected_diffs_still_never_leaks_under_the_default_ladders_own_policy(
    tmp_path: Path,
) -> None:
    """§12.35's own headline property, isolated from whether the payload FIELD is populated.

    Found by round GG task 4's own task review: the test above moves TWO variables at once —
    `context_policy` and `prior_rejected_diffs` — so nothing yet proved which one gates the leak.
    `DEFAULT_LADDER` (`models/tasks.py`) runs rung 3 as `EVIDENCE_PLUS_REJECTED_APPROACHES`, not
    `EVIDENCE_PLUS_PRIORS` — the policy this project actually ships by default. A one-token-class
    widening of `_evidence()`'s new gate (`is EVIDENCE_PLUS_PRIORS` -> `in (EVIDENCE_PLUS_
    REJECTED_APPROACHES, EVIDENCE_PLUS_PRIORS)`, the exact membership form the branch immediately
    above it already uses — a plausible "reconcile the two adjacent branches" edit) passed every
    existing test in this file silently, because none of them populates `prior_rejected_diffs`
    under any policy OTHER than `EVIDENCE_PLUS_PRIORS`. This is CLAUDE.md's "a fixture lacking the
    blocker certifies the defect GREEN" -- the SAME payload as the positive control above, with
    only `context_policy` flipped back to the policy the default ladder actually runs, closes it.
    """
    one, two, clean = f"{DEST}/one.py", f"{DEST}/two.py", f"{DEST}/clean.py"
    committed = "L1\nL2\nL3\nL4\n"
    dirty = "L1 DIRTY\nL2\nL3\nL4\n"
    after = (
        "REJECTED_LINE_1_UNIQUE\nREJECTED_LINE_2_UNIQUE\n"
        "REJECTED_LINE_3_UNIQUE\nREJECTED_LINE_4_UNIQUE\n"
    )
    repo, anchor = make_repo(tmp_path, {one: committed, two: committed, clean: "keep\n"})

    rejected_diff = make_unified_diff(one, dirty, after)
    assert rejected_diff, "fixture sanity: the rule actually changes the file"
    rejected_lines = [line for line in rejected_diff.splitlines() if line.strip()]
    assert len(rejected_lines) >= 8, "fixture sanity: a genuine multi-line diff, not one line"

    engine = FakeRewriter({"r1": lambda _source: after})
    worker = worker_with(engine)

    (repo / one).write_text(dirty, encoding="utf-8")
    attempt_two = asyncio.run(
        worker.run(make_ctx(repo, attempt=2), rewrite_payload(anchor, [one]))
    )
    assert attempt_two.status == "failed" and attempt_two.error is not None

    (repo / two).write_text(dirty, encoding="utf-8")
    client = FakeModelClient(_proposal(clean, "keep\n", "kept\n", marker="repair"))
    ctx = make_ctx(
        repo,
        attempt=3,
        tier=TransformTier.LLM_REPAIR,
        # the default ladder's own rung-3 policy
        context_policy=ContextPolicy.EVIDENCE_PLUS_REJECTED_APPROACHES,
        llm=client,
    )
    # The IDENTICAL payload the positive control uses -- `prior_rejected_diffs` populated exactly
    # the same way. Only `ctx.context_policy` differs. If a future edit widens the render gate's
    # membership test, this is what catches it.
    payload = rewrite_payload(
        anchor,
        [two],
        prior_rejected_diffs=[
            FilePatch(
                path=one,
                diff=rejected_diff,
                tier=TransformTier.DETERMINISTIC,
                parse_probe_ok=False,
            )
        ],
    )

    result = asyncio.run(worker.run(ctx, payload))

    assert client.roles == [str(Role.TRANSFORM_REPAIR)], "attempt 3 is the WORKHORSE rung"
    prompt = client.prompts[0]

    for line in rejected_lines:
        assert line not in prompt, (
            f"a prior rejected diff line leaked under EVIDENCE_PLUS_REJECTED_APPROACHES despite "
            f"prior_rejected_diffs being populated -- the gate must key on policy, not payload "
            f"content: {line!r}"
        )

    assert result.status == "ok", "the repair patch still landed"
    assert (repo / clean).read_text(encoding="utf-8") == "kept\n"


def test_a_multi_file_repair_records_every_landed_path_not_just_the_unit(
    tmp_path: Path,
) -> None:
    """D49 leg 3: `LlmPatchProposal.files` allows up to 64 entries (`llm/schemas.py:199`), and
    `land_patches` commits every one of them in the SAME commit (`patch_id` is over the whole
    sequence). Recording only `unit` would under-report a multi-file repair by 63 files at worst —
    `_transform_criterion`'s §3.2 parse probe reads exactly `output.rewritten`, so an unrecorded
    landed file is a parse failure that ships unprobed.
    """
    unit = f"{DEST}/mod.py"
    sibling = f"{DEST}/sibling.py"
    repo, anchor = make_repo(tmp_path, {unit: "alpha\n", sibling: "keep\n"})
    worker = worker_with(FakeRewriter({}))  # no rule fires: RULE_MISS, the rung-2 case
    client = FakeModelClient(
        LlmPatchProposal(
            files=(
                ProposedFileEdit(path=unit, diff=make_unified_diff(unit, "alpha\n", "beta\n")),
                ProposedFileEdit(
                    path=sibling, diff=make_unified_diff(sibling, "keep\n", "kept\n")
                ),
            ),
            approach_summary="mod.py's failure traces to a stale constant in its sibling",
            rationale="the deterministic rule cannot span two files; the model can",
        )
    )
    ctx = make_ctx(
        repo,
        attempt=2,
        tier=TransformTier.LLM_REPAIR,
        context_policy=ContextPolicy.EVIDENCE_ONLY,
        llm=client,
    )

    result = asyncio.run(worker.run(ctx, rewrite_payload(anchor, [unit])))

    assert result.status == "ok"
    assert (repo / unit).read_text(encoding="utf-8") == "beta\n"
    assert (repo / sibling).read_text(encoding="utf-8") == "kept\n"
    assert result.output is not None
    assert sorted(result.output.rewritten) == sorted([unit, sibling]), (
        "both landed paths are recorded — a bare `[unit]` would silently drop `sibling`, the "
        "genuinely-written file that would then go unprobed by the §3.2 parse-probe loop"
    )
    assert len(log_entries(repo, anchor)) == 1, "both files land in the same repair commit"


def test_the_escalation_rung_carries_rejected_approach_summaries_and_can_ask_for_a_human(
    tmp_path: Path,
) -> None:
    """Attempt 3 gets search-space pruning without the dead diffs, and may say "human".

    Why it matters: `abandon_recommended` is the only way for the last rung to reach
    `REQUIRES_HUMAN_INTERVENTION` by being TOLD rather than by burning the retry budget — and the
    summaries it is shown carry no diff text, because `RejectedApproach` has no field for one.
    """
    unit = f"{DEST}/mod.py"
    repo, anchor = make_repo(tmp_path, {unit: "alpha\n"})
    worker = worker_with(FakeRewriter({}))  # no rule fires: RULE_MISS, the unresolved case
    client = FakeModelClient(
        LlmEscalationProposal(
            files=(ProposedFileEdit(path=unit, diff="diff --git a/x b/x\n"),),
            approach_summary="cannot be done deterministically",
            rationale="the module this import needs is not on the deps path",
            abandon_recommended=True,
            human_intervention_reason="the dependency is not in the monorepo at all",
        )
    )
    ctx = make_ctx(
        repo,
        attempt=3,
        tier=TransformTier.LLM_ESCALATION,
        context_policy=ContextPolicy.EVIDENCE_PLUS_REJECTED_APPROACHES,
        llm=client,
    )
    # The real multi-line diff the rejected rung-2 approach would have proposed, captured with the
    # SAME helper (`make_unified_diff`) the production pipeline calls internally — a genuine diff,
    # not a hand-typed guess. `RejectedApproach` has no field to carry it (the docstring above), so
    # this represents what *would* leak if that boundary were ever crossed.
    rejected_diff = make_unified_diff(
        unit,
        "alpha\n",
        "ABANDON_REJECTED_LINE_1\nABANDON_REJECTED_LINE_2\n"
        "ABANDON_REJECTED_LINE_3\nABANDON_REJECTED_LINE_4\n",
    )
    assert rejected_diff, "fixture sanity: the diff is non-empty"
    rejected_lines = [line for line in rejected_diff.splitlines() if line.strip()]
    assert len(rejected_lines) >= 8, "fixture sanity: a genuine multi-line diff, not one line"
    payload = rewrite_payload(
        anchor,
        [unit],
        rules=[],
        rejected_approaches=[
            RejectedApproach(
                approach_signature="c" * 64,
                reason="REJECTED_APPROACH_SUMMARY_MARKER",
                failure_class=FailureClass.RULE_MISS,
                attempt=2,
                tier=TransformTier.LLM_REPAIR,
            )
        ],
    )

    result = asyncio.run(worker.run(ctx, payload))

    assert client.roles == [str(Role.ESCALATION)]
    prompt = client.prompts[0]
    assert "REJECTED_APPROACH_SUMMARY_MARKER" in prompt, "the HEAVY rung gets the pruning"
    for line in rejected_lines:
        assert line not in prompt, f"a rejected diff line leaked into the prompt: {line!r}"
    assert result.status == "failed" and result.error is not None
    assert result.error.retryable is False, "told, not discovered by exhausting the budget"
    assert "recommends a human" in result.error.stderr_tail
    assert log_entries(repo, anchor) == []


def test_the_repair_rung_makes_a_real_call_through_the_context_and_lands_what_it_gets(
    tmp_path: Path,
) -> None:
    """THE headline: at rung 2 the worker completes a typed call through `ctx.llm` and commits it.

    Why it matters: this could not happen at all. `WorkerContext.llm` was the `LlmRouter` — role →
    tier, no `complete()` — so the rung had nothing to call, and rather than pretend the
    deterministic rung was the whole ladder it reported a wiring failure through a structural
    `model_client_of()` probe. ADR-0014's repair rung was therefore unreachable under real
    wiring: every `RULE_MISS` in the fleet terminated at rung 1. `ctx.llm` is now the `ModelClient`
    itself, so the assertion is end-to-end — the prompt reaches a transport, the reply comes back
    validated into `LlmPatchProposal`, and the patch it carries becomes a commit on the branch.
    """
    unit = f"{DEST}/mod.py"
    repo, anchor = make_repo(tmp_path, {unit: "alpha\n"})
    worker = worker_with(FakeRewriter({}))  # no rule fires: RULE_MISS, the rung-2 case
    backend = ScriptedBackend(
        LlmPatchProposal(
            files=(
                ProposedFileEdit(path=unit, diff=make_unified_diff(unit, "alpha\n", "beta\n")),
            ),
            approach_summary="no rule covers this import; rewrite it directly",
            rationale="the deterministic engines have no pattern for this construct",
        )
    )
    ctx = make_ctx(
        repo,
        attempt=2,
        tier=TransformTier.LLM_REPAIR,
        context_policy=ContextPolicy.EVIDENCE_ONLY,
        llm=ladder_client(backend),
    )

    result = asyncio.run(worker.run(ctx, rewrite_payload(anchor, [unit], rules=[])))

    assert len(backend.prompts) == 1, "the rung reached the transport, not a wiring error"
    assert "mod.py" in backend.prompts[0], "and it carried the failure it is repairing"
    assert result.status == "ok", "the validated proposal landed as a commit"
    assert (repo / unit).read_text(encoding="utf-8") == "beta\n"
    assert result.completed_units == [unit]
    assert result.usage.input_tokens == 200, "the rung's spend is real and reported"
    entries = log_entries(repo, anchor)
    assert len(entries) == 1 and entries[0]["subject"].endswith(unit)
    assert result.output is not None and result.output.rewritten == [unit], (
        "D49 leg 3, LLM branch: the model's `path` matches `unit` here — the no-regression case "
        "for the common repair shape, alongside the mismatch case exercised elsewhere"
    )


def test_the_escalation_rung_reaches_the_model_with_its_context_policy_applied(
    tmp_path: Path,
) -> None:
    """Rung 3 completes a real HEAVY call too, and ADR-0021's policy shapes what it is shown.

    Why it matters: the rung that is allowed to say "a human is needed" was behind the same wall
    as rung 2, so a repo that needed escalation was abandoned having never once been shown to a
    model. Reaching the transport is only half of it: `EVIDENCE_PLUS_REJECTED_APPROACHES` must put
    the prior approach's *summary* in the prompt and keep its *diff* out, and that is now asserted
    where the prompt actually arrives rather than at a fake client's doorstep.
    """
    unit = f"{DEST}/mod.py"
    repo, anchor = make_repo(tmp_path, {unit: "alpha\n"})
    worker = worker_with(FakeRewriter({}))
    backend = ScriptedBackend(
        LlmEscalationProposal(
            files=(
                ProposedFileEdit(path=unit, diff=make_unified_diff(unit, "alpha\n", "gamma\n")),
            ),
            approach_summary="the import must move packages",
            rationale="the dependency was relocated by an earlier wave",
            abandon_recommended=False,
        )
    )
    ctx = make_ctx(
        repo,
        attempt=3,
        tier=TransformTier.LLM_ESCALATION,
        context_policy=ContextPolicy.EVIDENCE_PLUS_REJECTED_APPROACHES,
        llm=ladder_client(backend),
    )
    # The real multi-line diff the rejected rung-2 approach would have proposed, captured with the
    # SAME helper (`make_unified_diff`) the production pipeline calls internally — a genuine diff,
    # not a hand-typed guess. `RejectedApproach` has no field to carry it, so this represents what
    # *would* leak if that boundary were ever crossed.
    rejected_diff = make_unified_diff(
        unit,
        "alpha\n",
        "HEAVY_REJECTED_LINE_1\nHEAVY_REJECTED_LINE_2\n"
        "HEAVY_REJECTED_LINE_3\nHEAVY_REJECTED_LINE_4\n",
    )
    assert rejected_diff, "fixture sanity: the diff is non-empty"
    rejected_lines = [line for line in rejected_diff.splitlines() if line.strip()]
    assert len(rejected_lines) >= 8, "fixture sanity: a genuine multi-line diff, not one line"
    payload = rewrite_payload(
        anchor,
        [unit],
        rules=[],
        rejected_approaches=[
            RejectedApproach(
                approach_signature="d" * 64,
                reason="REJECTED_APPROACH_SUMMARY_MARKER",
                failure_class=FailureClass.RULE_MISS,
                attempt=2,
                tier=TransformTier.LLM_REPAIR,
            )
        ],
    )

    result = asyncio.run(worker.run(ctx, payload))

    assert len(backend.prompts) == 1, "rung 3 reached the transport"
    prompt = backend.prompts[0]
    assert "REJECTED_APPROACH_SUMMARY_MARKER" in prompt, "the HEAVY rung gets the pruning"
    for line in rejected_lines:
        assert line not in prompt, f"a rejected diff line leaked into the prompt: {line!r}"
    assert result.status == "ok"
    assert (repo / unit).read_text(encoding="utf-8") == "gamma\n"


def test_a_rung_the_budget_cannot_pay_for_never_reaches_the_transport(tmp_path: Path) -> None:
    """`BudgetExhausted` is raised before dispatch, and the rung fails loudly rather than quietly.

    Why it matters: §11.2 is fail-closed, and the gate lives inside `complete()` because that is
    the only place the target's price is known. Routing the ladder through the real client rather
    than a bespoke one is what keeps the gate on the path the rung uses — and `WorkerRepairError`
    rather than a swallowed `None` is what stops an unaffordable rung from looking exactly like a
    rung that ran and found nothing (Rule 11).
    """
    unit = f"{DEST}/mod.py"
    repo, anchor = make_repo(tmp_path, {unit: "alpha\n"})
    worker = worker_with(FakeRewriter({}))
    backend = ScriptedBackend(
        LlmPatchProposal(
            files=(
                ProposedFileEdit(path=unit, diff=make_unified_diff(unit, "alpha\n", "beta\n")),
            ),
            approach_summary="unaffordable",
            rationale="this call must never be dispatched",
        )
    )
    ctx = make_ctx(
        repo,
        attempt=2,
        tier=TransformTier.LLM_REPAIR,
        context_policy=ContextPolicy.EVIDENCE_ONLY,
        llm=ladder_client(backend),
    )
    ctx.budget = CallBudget(
        remaining_tokens=200_000, remaining_usd=0.0, deadline=ctx.deadline
    )

    with pytest.raises(WorkerRepairError):
        asyncio.run(worker.run(ctx, rewrite_payload(anchor, [unit], rules=[])))

    assert backend.prompts == [], "spend is refused before the transport, never after"
    assert log_entries(repo, anchor) == []


def test_a_tier_outage_at_the_escalation_rung_surfaces_as_backend_unavailable(
    tmp_path: Path,
) -> None:
    """D133: the ESCALATION rung's `except LlmError: raise WorkerRepairError(...) from exc`
    (`rewrite.py:730-731`) wraps a real `TierUnavailable` in a plain `RuntimeError`. Driven
    through `execute()` — the same bounded wrapper `orchestrator/runner.py` actually calls,
    not `run()` directly — this used to come out `UNKNOWN` and *retryable*, charging the repo
    an attempt for an outage that was not its fault, instead of the run-terminal
    `BACKEND_UNAVAILABLE` §11.8 owes a dead tier.

    `TierUnavailableClient` raises a REAL `TierUnavailable` out of `ctx.llm.complete()` — the
    same call surface `escalate_repair` uses in production — so `WorkerRepairError` is the
    genuine exception this rung raises, not a hand-built stand-in for it.
    """
    unit = f"{DEST}/mod.py"
    repo, anchor = make_repo(tmp_path, {unit: "alpha\n"})
    worker = worker_with(FakeRewriter({}))  # no rule matches: the deterministic rung finds nothing
    ctx = make_ctx(
        repo,
        attempt=3,  # DEFAULT_LADDER[2] == EVIDENCE_PLUS_REJECTED_APPROACHES == LLM_ESCALATION
        llm=TierUnavailableClient(ModelTier.HEAVY, ("fake:heavy-1", "fake:heavy-2")),
    )

    execution = asyncio.run(
        worker.execute(ctx, rewrite_payload(anchor, [unit], rules=[]), max_attempts=3)
    )

    failure = execution.last_error
    assert failure is not None, "the rung must fail, not report success with no output"
    assert failure.failure_class is FailureClass.BACKEND_UNAVAILABLE, (
        f"misclassified as {failure.failure_class}"
    )
    assert failure.retryable is False
    assert failure.tier is ModelTier.HEAVY
    assert execution.status is RepoStatus.PENDING, "an outage is not this repo's fault (§11.8)"
    assert log_entries(repo, anchor) == [], "nothing lands when the escalation rung never answers"


# =======================================================================================
# 7. one commit per task, each carrying its own Fleet-Patch-Id
# =======================================================================================
def test_each_task_lands_exactly_one_commit_carrying_its_own_trailers(tmp_path: Path) -> None:
    """One commit per `TransformTask`, and the trailers are the index into it (§3.2 step 6).

    Why it matters: the trailers are what turn "did my work land?" into a git query answerable
    from a bare clone with no database. A batched commit, or a `Fleet-Task-Id` minted per attempt,
    makes the §3.2 step 6.4 crash query unanswerable.
    """
    units = [f"{DEST}/a.py", f"{DEST}/b.py", f"{DEST}/c.py"]
    repo, anchor = make_repo(tmp_path, dict.fromkeys(units, "alpha\n"))
    worker = worker_with(FakeRewriter({"r1": lambda t: t.replace("alpha", "ALPHA")}))
    ctx = make_ctx(repo)

    result = asyncio.run(worker.run(ctx, rewrite_payload(anchor, units)))

    assert result.status == "ok"
    entries = log_entries(repo, anchor)
    assert len(entries) == 3, "one commit per task, not one batched commit for the phase"
    assert [e["subject"] for e in entries] == [f"fleet(rewrite): {u}" for u in units]
    for unit, entry in zip(units, entries, strict=True):
        assert len(entry["patch_id"]) == 64, "a content-addressed idempotency key"
        assert entry["task_id"] == str(task_id_for(ctx, Phase.TRANSFORM, unit))
        changed = git(repo, "show", "--name-only", "--format=", entry["sha"]).split()
        assert changed == [unit], "each commit is exactly its own task's tree change"
    assert len({e["patch_id"] for e in entries}) == 3, "distinct content, distinct ids"


# =======================================================================================
# 8. rollback is per TASK, never per phase
# =======================================================================================
def test_a_failing_task_resets_to_its_own_anchor_and_keeps_earlier_commits(
    tmp_path: Path,
) -> None:
    """`git reset --hard tasks.pre_commit_sha`, not `phases.pre_commit_sha` (§3.2 step 6.5).

    Why it matters: resetting a crashed task to the PHASE anchor deletes the commits of earlier
    tasks whose rows are already `DONE` and will never re-run — the phase then passes its success
    criterion on a tree missing most of its rewrites. The debris a killed `git apply` leaves is
    cleaned; the earlier task's commit is not.
    """
    good, bad = f"{DEST}/good.py", f"{DEST}/bad.py"
    repo, anchor = make_repo(tmp_path, {good: "alpha\n", bad: "alpha\n"})

    def debris(path: str) -> None:
        if path == bad:
            (repo / "killed-apply.tmp").write_text("debris\n", encoding="utf-8")

    worker = worker_with(
        FakeRewriter({"r1": lambda t: t.replace("alpha", "ALPHA")}, on_apply=debris)
    )
    (repo / bad).write_text("alpha dirty\n", encoding="utf-8")  # worktree != index → git refuses

    result = asyncio.run(worker.run(make_ctx(repo), rewrite_payload(anchor, [good, bad])))

    assert result.status == "failed" and result.error is not None
    assert result.error.failure_class is FailureClass.PATCH_REJECTED
    assert result.completed_units == [good], "the landed task is reported, not hidden"

    entries = log_entries(repo, anchor)
    assert len(entries) == 1 and entries[0]["subject"] == f"fleet(rewrite): {good}"
    assert git(repo, "rev-parse", "HEAD") == entries[0]["sha"], "reset to THIS task's anchor"
    assert git(repo, "rev-parse", "HEAD") != anchor, "and never back to the phase anchor"
    assert not (repo / "killed-apply.tmp").exists(), "`clean -fdx` took the debris"
    assert (repo / good).read_text(encoding="utf-8") == "ALPHA\n", "earlier work survives"


# =======================================================================================
# 9. the shared commit helper is shared, and the deadline is honoured by both workers
# =======================================================================================
def test_relocate_is_partial_when_the_deadline_lands_mid_plan(tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch,
                                                              ) -> None:
    """`relocate` owes the same `partial` contract as `rewrite`; both poll the same clock.

    Why it matters: a relocation stopped halfway is a tree half at its old paths and half at its
    new ones. Only `completed_units` tells the next invocation which half, and `preconditions_hold`
    must then admit exactly the remainder.
    """
    sources = [f"com/x/F{i}.java" for i in range(4)]
    repo, anchor = make_repo(tmp_path, dict.fromkeys(sources, "class F {}\n"))
    worker = RelocateWorker()
    payload = RelocateInput(
        branch=BRANCH, phase_pre_commit_sha=anchor, dest_path="java", sources=sources
    )

    ctx = make_ctx(repo)
    monkeypatch.setattr(relocate_mod, "loop_now", clock_expiring_after(ctx.deadline, 2))
    result = asyncio.run(worker.run(ctx, payload))
    monkeypatch.undo()

    assert result.status == "partial"
    assert result.completed_units == sources[:2]
    assert result.remaining_units == sources[2:]
    assert len(log_entries(repo, anchor)) == 2

    resumed = payload.model_copy(update={"completed_units": result.completed_units})
    assert asyncio.run(worker.preconditions_hold(make_ctx(repo), resumed)) is True
    finished = asyncio.run(worker.run(make_ctx(repo), resumed))
    assert finished.status == "ok"
    assert len(log_entries(repo, anchor)) == 4, "two more commits, not four"
    assert not (repo / "java/java").exists()


# =======================================================================================
# 10. ADR-0021 §3.2 step 5 / §12.36: the anchoring guard
# =======================================================================================
class ScriptedRepairClient:
    """A `ModelClient` that answers with a QUEUE of canned proposals, one per call.

    Unlike `FakeModelClient` (one fixed value), this is what drives the anti-anchoring RE-ASK:
    the same rung calls the model more than once when a proposal anchors, and the second call
    must see a genuinely different scripted answer.
    """

    def __init__(self, values: Sequence[BaseModel]) -> None:
        self._queue = list(values)
        self.prompts: list[str] = []
        self.roles: list[str] = []

    async def complete[T: BaseModel](
        self,
        role: str,
        messages: Sequence[Message],
        response_model: type[T],
        *,
        tier_override: ModelTier | None = None,
        max_output_tokens: int | None = None,
        timeout_s: float | None = None,
        budget: CallBudget | None = None,
    ) -> ModelResponse[T]:
        self.roles.append(role)
        self.prompts.append("\n".join(message.content for message in messages))
        value = self._queue.pop(0)
        return ModelResponse(
            value=response_model.model_validate_json(value.model_dump_json()),
            usage=TokenUsage(role=role, input_tokens=100, output_tokens=20, cost_usd=0.02),
            mode=StructuredOutputMode.JSON_SCHEMA,
            finish_reason="stop",
        )

    async def _empty(self) -> AsyncIterator[StreamEvent]:
        return
        yield StreamEvent()  # pragma: no cover - never reached; makes this an async generator

    def stream[T: BaseModel](
        self,
        role: str,
        messages: Sequence[Message],
        response_model: type[T],
        *,
        budget: CallBudget | None = None,
    ) -> AsyncIterator[StreamEvent]:
        return self._empty().__aiter__()

    async def capabilities(self, role: str) -> ModelCapabilities:
        return ModelCapabilities()


def _escalation(target: str, diff: str, *, marker: str) -> LlmEscalationProposal:
    return LlmEscalationProposal(
        files=(ProposedFileEdit(path=target, diff=diff),),
        approach_summary=f"rewrite {target} ({marker})",
        rationale="the deterministic rule could not land; the model proposes a fix",
    )


def test_the_anchoring_guard_rejects_a_repeated_approach_before_any_probe_reasks_once_and_lands_a_genuinely_different_fix(  # noqa: E501
    tmp_path: Path,
) -> None:
    """SPEC's own §12.36 scenario, end to end at the worker layer.

    A signature already sits in `rejected_approaches` for this unit's task (simulating a prior
    rung's genuine failure). The escalation rung's FIRST call proposes "the same idea, retyped" —
    re-indented, with a shifted hunk offset — which must collide and be rejected before any
    worktree mutation; the re-ask (budget 1) then proposes a genuinely different fix, which must
    land normally.
    """
    target = f"{DEST}/repair_target.py"
    before = "import os\n\n\ndef handler():\n    return None\n"
    repo, anchor = make_repo(tmp_path, {target: before})
    worker = worker_with(FakeRewriter({}))  # no rule fires: RULE_MISS, the unresolved case

    # Fix A touches the `return` statement inside `handler` — an OTHER-kind change at symbol
    # "handler". Fix A v2 is the SAME idea, retyped: re-indented, an extra blank line, a shifted
    # hunk offset — none of which may move the fingerprint (docs/SPEC.md:912).
    fix_a_v1 = (
        f"--- a/{target}\n+++ b/{target}\n@@ -4,2 +4,2 @@\n"
        " def handler():\n-    return None\n+    return Money()\n"
    )
    fix_a_v2 = (
        f"--- a/{target}\n+++ b/{target}\n@@ -3,3 +3,4 @@\n"
        "+\n def handler():\n-    return None\n+    return   Money()\n"
    )
    # Fix B is genuinely different AT THE APPROACH LEVEL: an import-statement addition (a
    # different `change_kind`, IMPORT_REWRITE, at no enclosing symbol) rather than another tweak
    # to `handler`'s body — SPEC's ApproachElement is `(path, change_kind, target_symbol)`, so two
    # different one-line edits to the SAME symbol under the SAME kind are "the same approach" by
    # design; a genuinely different approach must differ in kind or symbol, not merely in content.
    after = "import os\nimport sys\n\n\ndef handler():\n    return None\n"
    fix_b = make_unified_diff(target, before, after)
    assert fix_b, "fixture sanity: a genuine, real diff"

    seeded_signature = asyncio.run(compute_approach_signature([fix_a_v1]))
    task_id = str(rewrite_mod.task_id_for_ids(RUN_ID, REPO_ID, int(Phase.TRANSFORM), target))
    db = FakeDb({task_id: frozenset({seeded_signature})})

    client = ScriptedRepairClient(
        [
            _escalation(target, fix_a_v2, marker="anchored repeat"),
            _escalation(target, fix_b, marker="genuinely different"),
        ]
    )
    ctx = make_ctx(
        repo,
        attempt=3,
        tier=TransformTier.LLM_ESCALATION,
        context_policy=ContextPolicy.EVIDENCE_PLUS_REJECTED_APPROACHES,
        llm=client,
        db=db,
    )
    payload = rewrite_payload(anchor, [target], rules=[], max_reasks_per_rung=1)

    result = asyncio.run(worker.run(ctx, payload))

    assert client.roles == [str(Role.ESCALATION), str(Role.ESCALATION)], (
        "one call, then exactly one re-ask — the budget"
    )
    assert result.status == "ok", "the genuinely different fix landed"
    assert result.output is not None

    # -- the collision: exactly one anchored rejection, recorded but not applied --------------
    rejections = result.output.anchored_rejections
    assert len(rejections) == 1
    rejection = rejections[0]
    assert rejection.approach_signature == seeded_signature, (
        "two identical approach_signatures — the re-indented/re-ordered/offset-shifted repeat "
        "fingerprints identically to the one already in rejected_approaches"
    )
    assert rejection.unit == target
    assert rejection.usage.cost_usd > 0, (
        "non-zero cost_usd on the rejected call: the LLM call that produced it still cost "
        "something even though detection is free"
    )

    # -- zero worktree mutation / zero container starts for the rejected candidate -------------
    entries = log_entries(repo, anchor)
    assert len(entries) == 1, "exactly ONE commit: the genuinely different fix, never the repeat"
    assert (repo / target).read_text(encoding="utf-8") == (
        "import os\nimport sys\n\n\ndef handler():\n    return None\n"
    )

    # -- the second (fresh) signature is different and is what actually landed -----------------
    landed_signature = asyncio.run(compute_approach_signature([fix_b]))
    assert landed_signature != seeded_signature


def test_no_anchoring_guard_applies_the_repeat_and_records_a_guard_off_event(
    tmp_path: Path,
) -> None:
    """`--no-anchoring-guard` (`payload.anchoring_enabled=False`): a collision is applied anyway,
    and the run's findings record the guard was off (§10, ADR-0021)."""
    target = f"{DEST}/repair_target.py"
    repo, anchor = make_repo(tmp_path, {target: "def handler():\n    return None\n"})
    worker = worker_with(FakeRewriter({}))

    fix_a_v1 = (
        f"--- a/{target}\n+++ b/{target}\n@@ -1,2 +1,2 @@\n"
        " def handler():\n-    return None\n+    return Money()\n"
    )
    fix_a_v2 = make_unified_diff(
        target, "def handler():\n    return None\n", "def handler():\n    return Money()\n"
    )
    seeded_signature = asyncio.run(compute_approach_signature([fix_a_v1]))
    task_id = str(rewrite_mod.task_id_for_ids(RUN_ID, REPO_ID, int(Phase.TRANSFORM), target))
    db = FakeDb({task_id: frozenset({seeded_signature})})

    client = ScriptedRepairClient([_escalation(target, fix_a_v2, marker="repeat, guard off")])
    ctx = make_ctx(
        repo,
        attempt=3,
        tier=TransformTier.LLM_ESCALATION,
        context_policy=ContextPolicy.EVIDENCE_PLUS_REJECTED_APPROACHES,
        llm=client,
        db=db,
    )
    payload = rewrite_payload(anchor, [target], rules=[], anchoring_enabled=False)

    result = asyncio.run(worker.run(ctx, payload))

    assert client.roles == [str(Role.ESCALATION)], (
        "no re-ask: the collision was applied, not rejected"
    )
    assert result.status == "ok"
    assert result.output is not None
    assert result.output.anchored_rejections == [], "guard off: nothing was REJECTED"
    assert len(result.output.guard_off) == 1
    event = result.output.guard_off[0]
    assert event.unit == target
    assert event.approach_signature == seeded_signature
    assert len(log_entries(repo, anchor)) == 1, "the repeat WAS applied"
    assert (repo / target).read_text(encoding="utf-8") == "def handler():\n    return Money()\n"


def test_a_genuinely_rejected_proposal_is_recorded_as_a_failed_approach(tmp_path: Path) -> None:
    """ADR-0021: a proposal that reaches `check_diff` and is genuinely rejected (never merely
    anchored) is what `rejected_approaches` exists to remember for a LATER rung — its signature is
    carried on `output.failed_approaches`, distinct from `anchored_rejections`."""
    target = f"{DEST}/repair_target.py"
    repo, anchor = make_repo(tmp_path, {target: "alpha\n"})
    worker = worker_with(FakeRewriter({}))
    out_of_tree_diff = make_unified_diff(
        "OUTSIDE/escape.py", "alpha\n", "ALPHA\n"
    )
    client = ScriptedRepairClient(
        [_escalation("OUTSIDE/escape.py", out_of_tree_diff, marker="escapes the subtree")]
    )
    ctx = make_ctx(
        repo,
        attempt=3,
        tier=TransformTier.LLM_ESCALATION,
        context_policy=ContextPolicy.EVIDENCE_ONLY,
        llm=client,
    )
    payload = rewrite_payload(anchor, [target], rules=[])

    result = asyncio.run(worker.run(ctx, payload))

    assert result.status == "failed"
    assert result.output is not None
    assert result.output.anchored_rejections == [], "this was never a collision"
    assert len(result.output.failed_approaches) == 1
    failed = result.output.failed_approaches[0]
    assert failed.unit == target
    assert failed.failure_class == FailureClass.PATCH_REJECTED
    assert len(failed.approach_signature) == 64
    assert len(log_entries(repo, anchor)) == 0, "check_diff rejects before any commit"
