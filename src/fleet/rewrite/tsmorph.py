"""Fenced secondary engine: a Node subprocess bridge for type-aware TS rewrites (ADR-0006).

"Fenced" means it may only be selected by a `RewriteRule` declaring `engine: ts-morph` and a
TypeScript language id; it is never a fallback for another language.

Availability here is **two** facts, and both are checked, because either one missing produces the
same silent nothing: a `node` executable, and a resolvable `ts-morph` package. A host with Node
but no `ts-morph` is the common case (this one is), so probing only for `node` would let a rule
"run" and rewrite nothing.

**Absence is an error, not a no-op** (Rule 11): `apply` raises `EngineUnavailableError` naming
whichever half is missing. State of the implementation, stated plainly: the availability gate and
the fence are real; the bridge script itself is not shipped, so with `ts-morph` present `apply`
raises `NotImplementedError` rather than pretending to have rewritten anything.
"""

from __future__ import annotations

import shutil

from fleet.models.tasks import FilePatch
from fleet.rewrite.rules import EngineUnavailableError, RewriteRule
from fleet.util.proc import CommandRunner, run

__all__ = ["REWRITER", "TsMorphRewriter"]

_PACKAGE = "ts-morph"
_FENCE_LANGUAGES = frozenset({"typescript", "tsx"})


class TsMorphRewriter:
    """The ts-morph bridge behind the §7.4 `Rewriter` protocol."""

    engine = "ts-morph"

    def __init__(
        self,
        *,
        node_binary: str = "node",
        runner: CommandRunner = run,
        timeout_s: float = 60.0,
    ) -> None:
        self.node_binary = node_binary
        self._runner = runner
        self.timeout_s = timeout_s

    async def available(self) -> bool:
        """Node present AND `ts-morph` resolvable from it. Async because the second half is a
        question only Node can answer."""
        if shutil.which(self.node_binary) is None:
            return False
        result = await self._runner(
            [self.node_binary, "-e", "require.resolve('ts-morph')"], timeout_s=self.timeout_s
        )
        return result.ok

    async def ensure_available(self) -> None:
        if shutil.which(self.node_binary) is None:
            raise EngineUnavailableError(
                f"rewrite engine 'ts-morph' is unavailable: no {self.node_binary!r} executable "
                f"on PATH — install Node, or stop routing rules at engine 'ts-morph'"
            )
        if not await self.available():
            raise EngineUnavailableError(
                f"rewrite engine 'ts-morph' is unavailable: Node cannot resolve the "
                f"{_PACKAGE!r} package — `npm install ts-morph`, or stop routing rules at "
                f"engine 'ts-morph'"
            )

    def _check_fence(self, rule: RewriteRule) -> None:
        outside = sorted(set(rule.languages) - _FENCE_LANGUAGES)
        if outside:
            raise ValueError(
                f"rule {rule.id!r} selects engine 'ts-morph' for {outside}; the ADR-0006 fence "
                f"restricts this engine to TypeScript"
            )

    async def apply(
        self, rule: RewriteRule, path: str, source: str, params: dict[str, str]
    ) -> FilePatch | None:
        self._check_fence(rule)
        await self.ensure_available()
        raise NotImplementedError(
            f"rule {rule.id!r}: the ts-morph bridge script is not shipped, so no type-aware "
            f"rewrite can be performed — route the rule at engine 'ast-grep' (path {path!r}, "
            f"{len(source)} bytes, {len(params)} params)."
        )

    async def parse_probe(self, path: str) -> bool:
        await self.ensure_available()
        raise NotImplementedError(
            f"the ts-morph bridge script is not shipped, so {path!r} cannot be probed by this "
            f"engine — use the ast-grep driver's probe"
        )


REWRITER: TsMorphRewriter = TsMorphRewriter()
"""The instance `transform.engines` resolves to (`fleet.rewrite.tsmorph`)."""
