"""D137: the real escalation ladder must actually call `CachingModelClient.scoped()`.

ADR-0021 makes `context_policy` / `rejected_approach_digest` components of the `llm_cache` key
so that two rungs which happen to render byte-identical prompt text under two *different*
declared context policies do not silently collide on one cache entry. `CachingModelClient.scoped()`
(`fleet.llm.cache`) is the mechanism that binds those two values into a fresh client view — but
until D137's fix, nothing in `src/fleet/` ever called it: `RewriteWorker._repair()` read the one
shared, unscoped `ctx.llm` for every rung, every attempt, every run.

Per `tests/test_run_context_llm_cache.py`'s own discipline (its module docstring), this asserts on
**rows written to the `llm_cache` table** — never on `isinstance`/field identity — because a
wrapped-but-unconsulted client is indistinguishable from a correctly-scoped one by inspection. It
reuses that file's real-`RunContext`/real-temp-SQLite/`LoggingBackend` fixture pattern rather than
building a parallel one.

Two rungs are simulated exactly as `BaseWorker.execute()`'s `replace()` would produce them
(`workers/base.py:807-812`): a `WorkerContext` built once via `RunContext.worker_context()`, then
`dataclasses.replace`d per rung. `RewriteWorker._repair()` is called directly against each rung's
context — the offline `LoggingBackend` answers both `transform_repair` and `escalation` role calls
with the same schema-valid JSON, since `LlmEscalationProposal` is `LlmPatchProposal` plus two
fields that carry their own defaults.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from fleet.llm.cache import EMPTY_SHA256
from fleet.llm.client import BackendReply, CallBudget, Message, StructuredOutputMode
from fleet.models.enums import ContextPolicy, FailureClass, Phase, TransformTier
from fleet.models.tasks import BackendTarget, RejectedApproach, TokenUsage
from fleet.orchestrator.context import RunContext
from fleet.util.hashing import cache_key
from fleet.workers.rewrite import RewriteInput, RewriteWorker
from tests.test_run_context_llm_cache import LoggingBackend, _config, _context

REPO_ID = "repo-d137"
SIGNATURE = "a" * 64


class LoggingLadderBackend(LoggingBackend):
    """Answers `transform_repair` and `escalation` alike with one schema-valid `LlmPatchProposal`
    shape — `LlmEscalationProposal` adds only fields with their own defaults, so the same JSON
    validates against both response models. Subclasses `LoggingBackend` (rather than duplicating
    it) purely so `_context`'s `backend:` parameter type is satisfied; `__init__`/`calls`/
    `declared_capabilities` are inherited unchanged, only `invoke`'s reply shape differs."""

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
        self.calls.append(target.model_id)
        payload = {
            "files": [{"path": "pkg/mod.py", "diff": "--- a\n+++ b\n@@\n-x\n+y\n"}],
            "approach_summary": "rename the call site",
            "rationale": "the probe named this exact symbol as unresolved",
        }
        return BackendReply(
            text=json.dumps(payload),
            usage=TokenUsage(input_tokens=10, output_tokens=3, model_id=target.model_id),
            finish_reason="stop",
        )


def _payload() -> RewriteInput:
    return RewriteInput(
        branch="migrate/repo-d137",
        phase_pre_commit_sha="0" * 40,
        dest_path="repos/repo-d137",
        rejected_approaches=[
            RejectedApproach(
                approach_signature=SIGNATURE,
                reason="wrong target module",
                failure_class=FailureClass.RULE_MISS,
                attempt=1,
                tier=TransformTier.LLM_REPAIR,
            )
        ],
    )


async def _rows(ctx: RunContext) -> list[tuple[str | None, str]]:
    async with ctx.read_conn.execute(
        "SELECT context_policy, rejected_approach_digest FROM llm_cache ORDER BY rowid"
    ) as cursor:
        return [(row[0], row[1]) for row in await cursor.fetchall()]


async def test_two_rungs_with_different_policies_bind_distinct_cache_keys(
    tmp_path: Path,
) -> None:
    """The direct operationalization of D137's own "Failure this permits" scenario: two rungs
    that render the same evidence under two different `ContextPolicy` values must land two
    distinct `llm_cache` rows, one per declared policy/digest pair — never a single collapsed
    row.

    DISCRIMINATES against `RewriteWorker._repair()` reading the bare, unscoped `ctx.llm`: under
    that reversion, `CachingModelClient.__init__`'s own default (`context_policy=None`) means
    both rows read `context_policy IS NULL`, `rejected_approach_digest == EMPTY_SHA256` — this
    test's row-A/row-B assertions on the string values fail.
    """
    backend = LoggingLadderBackend()
    async with _context(tmp_path, config=_config(), backend=backend) as run_ctx:
        base_worker_ctx = run_ctx.worker_context(
            repo_id=REPO_ID,
            phase=Phase.TRANSFORM,
            attempt=2,
            lease_fence=1,
            cancel=asyncio.Event(),
            budget=CallBudget(
                remaining_tokens=100_000,
                remaining_usd=5.0,
                deadline=asyncio.get_running_loop().time() + 60,
            ),
        )
        rung_a = replace(
            base_worker_ctx,
            attempt=2,
            tier=TransformTier.LLM_REPAIR,
            context_policy=ContextPolicy.EVIDENCE_ONLY,
        )
        rung_b = replace(
            base_worker_ctx,
            attempt=3,
            tier=TransformTier.LLM_ESCALATION,
            context_policy=ContextPolicy.EVIDENCE_PLUS_REJECTED_APPROACHES,
        )

        worker = RewriteWorker()
        payload = _payload()
        evidence = (FailureClass.RULE_MISS, "probe output naming the symbol", "stderr text")

        repair_a = await worker._repair(
            rung_a, payload, unit="pkg/mod.py", source="x = 1\n", evidence=evidence
        )
        repair_b = await worker._repair(
            rung_b, payload, unit="pkg/mod.py", source="x = 1\n", evidence=evidence
        )

        rows = await _rows(run_ctx)

    assert repair_a is not None and repair_b is not None
    assert len(rows) == 2, (
        f"expected two distinct llm_cache rows (one per rung), got {rows} — a collapsed single "
        "row means the two declared policies keyed identically"
    )
    (policy_a, digest_a), (policy_b, digest_b) = rows
    assert policy_a == "EVIDENCE_ONLY", rows
    assert digest_a == EMPTY_SHA256, (
        "rung A's policy does not gate payload.rejected_approaches in, and there were no "
        f"extra_rejected_signatures, so its digest must be the empty-set sha256; got {rows}"
    )
    assert policy_b == "EVIDENCE_PLUS_REJECTED_APPROACHES", rows
    assert digest_b == cache_key(SIGNATURE), (
        f"rung B's policy gates payload.rejected_approaches in, so its digest must be the "
        f"sha256 over that one known signature; got {rows}"
    )
