"""Fenced secondary engine: Python-only lossless rewrites (ADR-0006).

"Fenced" means it may only be selected by a `RewriteRule` that declares `engine: libcst` and
`languages: [python]`; it is never a fallback for another language, and the fence is enforced
here rather than trusted to rule authors.

**Absence is an error, not a no-op** (Rule 11): with `libcst` uninstalled, `apply` raises
`EngineUnavailableError` naming the package. A `None` return would read as "this rule matched
nothing" and an unrewritten file would be committed as a success.

State of the implementation, stated plainly: the availability gate and the fence are real, and
`parse_probe` is a real libcst parse. The transform itself is **not implemented** — §7.4 specifies
`rule`/`fix` in ast-grep's schema only, and inventing a second rule schema here without libcst
installed to test it against would be speculation. It therefore raises, loudly, naming the gap.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
from pathlib import Path

from fleet.models.tasks import FilePatch
from fleet.rewrite.rules import EngineUnavailableError, RewriteRule

__all__ = ["REWRITER", "LibCstRewriter"]

_PACKAGE = "libcst"
_FENCE_LANGUAGES = frozenset({"python"})


def _read_text(path: str) -> str:
    """Off-loop file read: the probe is the one place this driver touches the filesystem, and a
    blocking read inside the event loop stalls every other worker sharing it (§11.1)."""
    return Path(path).read_text(encoding="utf-8")


class LibCstRewriter:
    """The libcst driver behind the §7.4 `Rewriter` protocol."""

    engine = "libcst"

    def available(self) -> bool:
        return importlib.util.find_spec(_PACKAGE) is not None

    def ensure_available(self) -> None:
        if not self.available():
            raise EngineUnavailableError(
                "rewrite engine 'libcst' is unavailable: the `libcst` package is not installed "
                "on this host — `pip install libcst`, or stop routing rules at engine 'libcst'"
            )

    def _check_fence(self, rule: RewriteRule) -> None:
        outside = sorted(set(rule.languages) - _FENCE_LANGUAGES)
        if outside:
            raise ValueError(
                f"rule {rule.id!r} selects engine 'libcst' for {outside}; the ADR-0006 fence "
                f"restricts this engine to Python"
            )

    async def apply(
        self, rule: RewriteRule, path: str, source: str, params: dict[str, str]
    ) -> FilePatch | None:
        self._check_fence(rule)
        self.ensure_available()
        raise NotImplementedError(
            f"rule {rule.id!r}: the libcst transform is not implemented — §7.4 defines "
            f"`rule`/`fix` in ast-grep's schema only, and this driver ships no second schema. "
            f"Route the rule at engine 'ast-grep' (path {path!r}, {len(source)} bytes, "
            f"{len(params)} params)."
        )

    async def parse_probe(self, path: str) -> bool:
        """Real: `libcst.parse_module` on the file's current bytes. Reading `path` is legitimate
        here — only `apply` must be pure, because only `apply` composes."""
        self.ensure_available()
        module = importlib.import_module(_PACKAGE)
        text = await asyncio.to_thread(_read_text, path)
        try:
            module.parse_module(text)
        except Exception:
            # Any libcst failure is the same verdict — it does not parse — and the verdict is the
            # answer to the question, not an error to propagate (§3.2 step 6.2).
            return False
        return True


REWRITER: LibCstRewriter = LibCstRewriter()
"""The instance `transform.engines` resolves to (`fleet.rewrite.libcst_py`)."""
