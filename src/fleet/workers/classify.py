"""Phase 1: LLM repo classification (service / library) via the CHEAP tier (§3.1, ADR-0008).

Advisory metadata only: nothing in the ordering, transformation or verification path may branch
on this worker's output (ADR-0008). That is why the confidence floor here *downgrades the label*
to `unknown` rather than failing the repo — a guess the harness does not trust is a guess it
records and moves past.

Two boundaries are deliberate:

* **The judgment goes through `ModelClient`, the evidence does not.** Which files a repo holds,
  which manifests it declares and which ecosystems those imply are facts a walk establishes
  (Rule 5), so they are gathered by code and handed to the model as evidence. The single thing
  the model is asked is the classification itself, and the answer comes back already validated
  into `llm.schemas.RepoClassification` — the schema is the validator (ADR-0002).
* **A malformed reply is a typed failure, not a crash.** Every `LlmError` — a reply that never
  satisfied the schema, an exhausted budget, a tier with no healthy target — becomes a structured
  `WorkerError` whose `failure_class` and `retryable` are what `retry.py` branches on. Nothing
  here matches on message text, and nothing here lets a `ValidationError` escape into the wave.

The client arrives on the **context** (§7.1), assembled once per run by `RunContext` from the
router, the backends and the cache (Guardrail 3). It is not also a constructor argument: two ways
to supply the same collaborator is two authorities for one fact, and the loser is always the one
the caller forgot — this worker used to answer `BACKEND_UNAVAILABLE` for a run whose client was
perfectly healthy, because the registry instantiates workers with no arguments. `ctx.router` is
still consulted, but only for the *tier* the role routes to, which is the `limits` semaphore key.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar, Final

from pydantic import Field, ValidationError

from fleet.llm.client import (
    BudgetExhausted,
    LlmError,
    MalformedReply,
    Message,
    SchemaUnsatisfied,
    TierUnavailable,
    TransportError,
)
from fleet.llm.roles import Role
from fleet.llm.schemas import RepoClassification
from fleet.models.enums import Ecosystem, FailureClass, Phase
from fleet.models.repo import RepoId
from fleet.obs.redact import redact_text
from fleet.orchestrator.registry import register_worker
from fleet.workers.base import (
    BaseWorker,
    WorkerContext,
    WorkerError,
    WorkerInput,
    WorkerOutput,
    WorkerResult,
    loop_now,
)
from fleet.workers.interrogate import DEFAULT_IGNORE_GLOBS, walk_files, worktree_of

__all__ = [
    "UNIT",
    "ClassifyInput",
    "ClassifyOutput",
    "ClassifyWorker",
]

UNIT: Final = "classify"
"""This worker's single unit of work. One repo, one judgment: there is nothing to resume INSIDE
a classification, so the unit list exists to make "already done" expressible, not to subdivide."""

_SYSTEM: Final = (
    "You classify source repositories for a monorepo migration. Answer only from the evidence "
    "given. If the evidence is thin, say so with a low confidence rather than guessing."
)

_MAX_OUTPUT_TOKENS: Final = 512


class ClassifyInput(WorkerInput):
    repo_id: RepoId
    worktree_path: str | None = None
    manifest_paths: tuple[str, ...] = Field(
        default=(), description="From the interrogate worker; the strongest ecosystem evidence"
    )
    ecosystems: tuple[Ecosystem, ...] = ()
    max_evidence_entries: int = Field(default=40, gt=0)
    min_confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Below this the label is recorded as `unknown` (§3.1 gates on confidence)",
    )
    remaining_units: tuple[str, ...] | None = Field(
        default=None, description="The checkpoint's owed units; None = no checkpoint"
    )


class ClassifyOutput(WorkerOutput):
    """`repos.kind` plus the evidence a reviewer needs to disagree with it."""

    repo_id: RepoId
    kind: str = Field(description="service | library | unknown — advisory only (ADR-0008)")
    ecosystem: Ecosystem = Ecosystem.UNKNOWN
    is_library: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = ""
    low_confidence: bool = False
    evidence_paths: tuple[str, ...] = ()


@register_worker
class ClassifyWorker(BaseWorker[ClassifyInput, ClassifyOutput]):
    """The one judgment call in Phase 1, routed through `ctx.llm` and validated (ADR-0002)."""

    __slots__ = ()

    name: ClassVar[str] = "classify"
    phase: ClassVar[Phase] = Phase.SCAN
    input_model: ClassVar[type[WorkerInput]] = ClassifyInput
    output_model: ClassVar[type[WorkerOutput]] = ClassifyOutput

    async def preconditions_hold(self, ctx: WorkerContext, payload: ClassifyInput) -> bool:
        """Has this repo already been classified against the tree that is still on disk?

        A classification is a judgment about a *worktree*, so the two facts that make re-entry
        safe are (a) a checkpoint recorded the unit as landed and (b) that worktree is still
        there. Fresh work has no checkpoint and answers False; a completed classification whose
        worktree has since been reaped also answers False, because the next answer would be about
        a tree nobody has read.
        """
        if payload.remaining_units is None or UNIT in payload.remaining_units:
            return False
        return await asyncio.to_thread(worktree_of(ctx, payload.worktree_path).is_dir)

    async def run(
        self, ctx: WorkerContext, payload: ClassifyInput
    ) -> WorkerResult[ClassifyOutput]:
        if payload.remaining_units is not None and UNIT not in payload.remaining_units:
            # Already landed under an earlier attempt: re-running would buy the same answer twice.
            return WorkerResult[ClassifyOutput](status="ok", completed_units=[UNIT])
        if ctx.cancelled() or ctx.expired(loop_now()):
            return WorkerResult[ClassifyOutput](
                status="cancelled",
                error=WorkerError(
                    failure_class=FailureClass.TIMEOUT,
                    retryable=True,
                    stderr_tail="cancelled before the classification was dispatched",
                ),
            )

        root = worktree_of(ctx, payload.worktree_path)
        evidence = await asyncio.to_thread(
            self._evidence, root, payload.manifest_paths, payload.max_evidence_entries
        )
        messages = self._messages(payload, evidence)

        try:
            tier = ctx.router.resolve(Role.REPO_CLASSIFY.value).tier
            async with ctx.limits.for_tier(tier):
                response = await ctx.llm.complete(
                    Role.REPO_CLASSIFY.value,
                    messages,
                    RepoClassification,
                    max_output_tokens=_MAX_OUTPUT_TOKENS,
                    budget=ctx.budget,
                )
        except (LlmError, ValidationError) as exc:
            return WorkerResult[ClassifyOutput](status="failed", error=_error_for(exc))

        answer = response.value
        trusted = answer.confidence >= payload.min_confidence
        return WorkerResult[ClassifyOutput](
            status="ok",
            output=ClassifyOutput(
                repo_id=payload.repo_id,
                kind=_kind_for(answer, trusted=trusted),
                ecosystem=answer.ecosystem,
                is_library=answer.is_library,
                confidence=answer.confidence,
                rationale=answer.rationale,
                low_confidence=not trusted,
                evidence_paths=tuple(evidence),
            ),
            completed_units=[UNIT],
            usage=response.usage,
            evidence=list(evidence),
        )

    # -- internals -----------------------------------------------------------------------

    def _evidence(
        self, root: Path, manifest_paths: Sequence[str], limit: int
    ) -> list[str]:
        """The deterministic half: manifests first, then the shallowest other paths (Rule 5).

        Manifests lead because they are the highest-signal evidence a repo offers about what it
        is, and the remainder is sorted by depth so a bounded prompt describes the top of the
        tree rather than an arbitrary corner of it.
        """
        paths = list(dict.fromkeys(manifest_paths))
        if root.is_dir():
            walked = walk_files(root, DEFAULT_IGNORE_GLOBS)
            rest = sorted(
                (rel for rel in walked if rel not in set(paths)),
                key=lambda rel: (rel.count("/"), rel),
            )
            paths.extend(rest)
        return paths[:limit]

    def _messages(self, payload: ClassifyInput, evidence: Sequence[str]) -> list[Message]:
        """The prompt. Repo-relative paths only — never a URL, never a remote, never a token."""
        ecosystems = ", ".join(sorted(eco.value for eco in payload.ecosystems)) or "none detected"
        listing = "\n".join(f"- {path}" for path in evidence) or "- (empty tree)"
        return [
            Message(role="system", content=_SYSTEM),
            Message(
                role="user",
                content=(
                    f"repo_id: {payload.repo_id}\n"
                    f"manifest ecosystems: {ecosystems}\n"
                    f"paths:\n{listing}\n\n"
                    "Classify this repository."
                ),
            ),
        ]


def _kind_for(answer: RepoClassification, *, trusted: bool) -> str:
    """`repos.kind` from the model's answer. A distrusted answer is `unknown`, not a coin flip."""
    if not trusted:
        return "unknown"
    return "library" if answer.is_library else "service"


def _error_for(exc: BaseException) -> WorkerError:
    """Map an LLM-layer failure onto the closed `FailureClass` vocabulary `retry.py` branches on.

    Every arm is a claim about whether ANOTHER attempt could plausibly differ: a reply that never
    satisfied the schema might (a re-ask is a different sample), an exhausted budget cannot
    (§11.2 is fail-closed), and a tier with no healthy target is terminal for the run (§11.8).
    """
    if isinstance(exc, BudgetExhausted):
        failure_class = FailureClass.BUDGET_EXHAUSTED
    elif isinstance(exc, TierUnavailable):
        failure_class = FailureClass.BACKEND_UNAVAILABLE
    elif isinstance(exc, TransportError):
        failure_class = FailureClass.TRANSIENT_INFRA
    elif isinstance(exc, SchemaUnsatisfied | MalformedReply | ValidationError):
        failure_class = FailureClass.PARSE_ERROR
    else:
        failure_class = FailureClass.UNKNOWN
    retryable = failure_class not in (
        FailureClass.BUDGET_EXHAUSTED,
        FailureClass.BACKEND_UNAVAILABLE,
    )
    return WorkerError(
        failure_class=failure_class,
        retryable=retryable,
        stderr_tail=redact_text(str(exc)),
        exception_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
    )
