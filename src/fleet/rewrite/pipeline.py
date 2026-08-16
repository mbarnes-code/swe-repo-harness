"""The deterministic rewrite pipeline (SPEC §7.4): buffer, order, fixpoint, conflict.

The engines are stateless drivers. **This module holds the buffer** — that division is the whole
design, and every property §7.4 asks for follows from it:

1. **Order is total.** Rules apply in `(priority, id)` order, so equal-priority rules cannot
   dispatch in YAML-load order.
2. **Each rule sees the previous rule's output text**, never the on-disk file. One in-memory
   buffer is threaded through the ordered rules and **one** `FilePatch` per file is emitted,
   computed against the file's committed state — so `git apply` runs once per file and two rules
   touching adjacent lines can never reject each other. (Before the signature change that gave
   `Rewriter.apply` a `source`, N rules each diffed one file against unspecified state; the second
   overlapping diff was rejected by `git apply`, and the escalation ladder then charged a YAML
   authoring error to the LLM repair budget.)
3. **Fixpoint, bounded.** The rule set re-runs over the buffer until it stops changing or
   `rewrite.max_passes` (default 3, §9) is spent; still changing at the ceiling is a
   `RuleOscillation` finding and the last pass-complete buffer ships.
4. **A genuine conflict is a rule defect, not a model's problem.** Two rules whose rewrites
   overlap on the same span produce a `RuleConflict` finding naming both rule ids and the span,
   the file is left unchanged, and the task **does not advance the ADR-0014 ladder**.

Spans are compared in *pass-start* coordinates. Each rule's edit is mapped back through the edits
that preceded it in the same pass, so "rule B rewrote the very text rule A just produced" is
detected as an overlap on A's span rather than passing as an independent edit.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Final, Literal

from fleet.models.enums import TransformTier
from fleet.models.tasks import FilePatch
from fleet.rewrite.apply import (
    DiffFormatError,
    Hunk,
    PatchApplyError,
    apply_in_memory,
    make_unified_diff,
    parse_unified_diff,
)
from fleet.rewrite.rules import EngineRegistry, RewriteRule, rule_matches_path, rule_sort_key

__all__ = [
    "DEFAULT_MAX_PASSES",
    "EngineContractError",
    "RewriteFinding",
    "RewriteOutcome",
    "RewritePipeline",
    "TextProbe",
]

DEFAULT_MAX_PASSES: Final = 3
"""§9 `transform.max_passes`. Mirrored, not imported from `FleetConfig`, so the pipeline is
constructible in a test without a config tree; the real value is passed in by the worker."""

TextProbe = Callable[[str, str], Awaitable[bool]]
"""`(path, text) -> parses?`. Injected (CLAUDE.md guardrail 3) because the probe belongs to the
engine and the pipeline must stay runnable with no engine installed. With no probe injected,
`FilePatch.parse_probe_ok` is `False` — "not yet proven", not "proven bad": `apply.apply_patch`
re-probes the file it actually wrote, and that is what a commit is gated on (§3.2 step 6.2)."""

#: 0-based half-open line range, in the coordinates of the buffer at the START of a pass.
Span = tuple[int, int]


class EngineContractError(RuntimeError):
    """A driver violated the `Rewriter` contract: a patch for another path, a multi-file diff, or
    a diff that does not apply to the very buffer it was handed (Rule 11).

    Loud, and never absorbed into a finding: a finding says "the rules disagree", which is an
    operator's problem to fix in YAML, while this says "the engine is broken", which is ours.
    """


@dataclass(frozen=True, slots=True)
class RewriteFinding:
    """One `findings` row raised by the rewrite pipeline, before the table exists in memory.

    §6 makes `findings.kind` free text on purpose, so this carries a `str`; `fingerprint` is the
    sha256 of the semantic identity, which is what makes a re-run re-raise the same finding
    without duplicating it (§11.7).
    """

    kind: str
    payload: Mapping[str, str]
    severity: Literal["warn", "error"] = "warn"
    repo_id: str | None = None

    @property
    def fingerprint(self) -> str:
        body = json.dumps(
            {
                "kind": self.kind,
                "repo_id": self.repo_id,
                "payload": dict(sorted(self.payload.items())),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RewriteOutcome:
    """One file's result. `patch` is the single composed diff, or `None` when nothing changed or
    a conflict left the file untouched."""

    path: str
    text: str
    patch: FilePatch | None = None
    findings: tuple[RewriteFinding, ...] = ()
    applied_rule_ids: tuple[str, ...] = ()
    passes: int = 0
    advance_ladder: bool = True
    """False *only* for `RuleConflict`: charging a YAML authoring error to the LLM repair budget
    would spend two escalation rungs and a `HEAVY` call to rediscover that two rules disagree."""

    @property
    def changed(self) -> bool:
        return self.patch is not None

    @property
    def conflicted(self) -> bool:
        return any(f.kind == "RuleConflict" for f in self.findings)

    @property
    def oscillated(self) -> bool:
        return any(f.kind == "RuleOscillation" for f in self.findings)


@dataclass(frozen=True, slots=True)
class _Conflict:
    first: str
    second: str
    span: Span


@dataclass(slots=True)
class _PassResult:
    text: str
    rule_ids: tuple[str, ...] = ()
    conflict: _Conflict | None = None


def _edits_of(hunk: Hunk) -> list[tuple[int, int, int]]:
    """`(old_start, old_end, post_line_count)` per contiguous changed run inside one hunk.

    Runs, not whole hunks: a unified diff carries three context lines either side, so claiming the
    hunk's full span would make two rules editing lines six apart "overlap" and fabricate a
    `RuleConflict` out of two perfectly composable rewrites.
    """
    edits: list[tuple[int, int, int]] = []
    old = hunk.old_start - 1 if hunk.old_len else hunk.old_start
    start = end = added = 0
    open_run = False
    for line in hunk.lines:
        tag = line[:1]
        if tag == "\\":
            continue
        if tag == " ":
            if open_run:
                edits.append((start, end, added))
                open_run = False
            old += 1
            continue
        if not open_run:
            start, end, added, open_run = old, old, 0, True
        if tag == "-":
            old += 1
            end = old
        elif tag == "+":
            added += 1
    if open_run:
        edits.append((start, end, added))
    return edits


def _origin_span(origin: Sequence[Span], start: int, end: int) -> Span:
    """Map a range of CURRENT buffer lines back to the pass-start coordinates it descends from."""
    if end > start:
        window = origin[start:end]
        return (min(s for s, _ in window), max(e for _, e in window))
    if start < len(origin):  # pure insertion, before an existing line
        return (origin[start][0], origin[start][0])
    if origin:  # pure insertion, at end of file
        return (origin[-1][1], origin[-1][1])
    return (0, 0)


def _overlap(a: Span, b: Span) -> bool:
    """Do two claims collide? Zero-width claims are insertions: two at the same point collide, and
    one inside another rule's rewritten span collides with it."""
    a_empty, b_empty = a[0] == a[1], b[0] == b[1]
    if a_empty and b_empty:
        return a[0] == b[0]
    if a_empty:
        return b[0] <= a[0] < b[1]
    if b_empty:
        return a[0] <= b[0] < a[1]
    return a[0] < b[1] and b[0] < a[1]


class RewritePipeline:
    """Owns the buffer, the ordering, the fixpoint loop, and conflict detection (§7.4).

    Construction is the startup gate: every rule's `engine` is resolved through the registry
    here, so an unresolvable engine raises `UnresolvedReferenceError` before any file is touched —
    exactly as an unknown `backend` does — rather than at the first file that happens to match it.
    """

    def __init__(
        self,
        rules: Sequence[RewriteRule],
        registry: EngineRegistry,
        *,
        max_passes: int = DEFAULT_MAX_PASSES,
        params: Mapping[str, str] | None = None,
        tier: TransformTier = TransformTier.DETERMINISTIC,
        probe: TextProbe | None = None,
        repo_id: str | None = None,
    ) -> None:
        if max_passes < 1:
            raise ValueError(f"max_passes must be >= 1, got {max_passes}")
        self.rules: tuple[RewriteRule, ...] = tuple(sorted(rules, key=rule_sort_key))
        self.registry = registry
        self.max_passes = max_passes
        self.params: dict[str, str] = dict(params or {})
        self.tier = tier
        self.probe = probe
        self.repo_id = repo_id
        registry.validate_rules(self.rules)

    def rules_for(self, path: str) -> tuple[RewriteRule, ...]:
        """The ordered rules claiming `path`. Public so a worker can log "no rule matched" as the
        distinct outcome it is, rather than as an empty patch."""
        return tuple(rule for rule in self.rules if rule_matches_path(rule, path))

    async def rewrite_file(
        self, path: str, source: str, *, params: Mapping[str, str] | None = None
    ) -> RewriteOutcome:
        """Run every matching rule to fixpoint over `source` and emit at most one `FilePatch`."""
        merged = {**self.params, **(params or {})}
        buffer = source
        applied: dict[str, None] = {}
        passes = 0
        findings: list[RewriteFinding] = []
        while passes < self.max_passes:
            passes += 1
            result = await self._run_pass(path, buffer, merged)
            if result.conflict is not None:
                return self._conflict_outcome(path, source, result.conflict, passes)
            if result.text == buffer:
                break
            buffer = result.text
            applied.update(dict.fromkeys(result.rule_ids))
        else:
            findings.append(
                RewriteFinding(
                    kind="RuleOscillation",
                    severity="warn",
                    repo_id=self.repo_id,
                    payload={
                        "path": path,
                        "max_passes": str(self.max_passes),
                        "rules": ",".join(sorted(applied)),
                    },
                )
            )
        return await self._finish(path, source, buffer, tuple(applied), passes, tuple(findings))

    async def rewrite_files(
        self, sources: Mapping[str, str], *, params: Mapping[str, str] | None = None
    ) -> tuple[RewriteOutcome, ...]:
        """Every file, in sorted path order — the order patches are then applied and journalled,
        which must not depend on dict insertion or on a directory walk."""
        return tuple(
            [
                await self.rewrite_file(path, sources[path], params=params)
                for path in sorted(sources)
            ]
        )

    # -- internals ----------------------------------------------------------------------
    async def _run_pass(self, path: str, buffer: str, params: Mapping[str, str]) -> _PassResult:
        """One ordered sweep of every matching rule over one buffer."""
        origin: list[Span] = [(i, i + 1) for i in range(len(buffer.splitlines(keepends=True)))]
        claims: list[tuple[str, Span]] = []
        fired: list[str] = []
        for rule in self.rules_for(path):
            rewriter = self.registry.require(rule.engine, rule_id=rule.id)
            # The run's params (from the RelocationPlan) win over the rule's own
            # defaults: `{{new_pkg}}` is a fact about THIS repo, not about the rule file.
            patch = await rewriter.apply(rule, path, buffer, {**rule.params, **params})
            if patch is None or not patch.diff:
                continue
            if patch.path != path:
                raise EngineContractError(
                    f"engine {rule.engine!r} (rule {rule.id!r}) returned a patch for "
                    f"{patch.path!r} while rewriting {path!r}"
                )
            edits, spans = self._claims_of(patch.diff, origin, rule)
            for other_id, other_span in claims:
                for span in spans:
                    if _overlap(span, other_span):
                        merged_span = (min(span[0], other_span[0]), max(span[1], other_span[1]))
                        return _PassResult(
                            text=buffer,
                            conflict=_Conflict(other_id, rule.id, merged_span),
                        )
            try:
                buffer = apply_in_memory(buffer, patch.diff)
            except (DiffFormatError, PatchApplyError) as exc:
                raise EngineContractError(
                    f"engine {rule.engine!r} (rule {rule.id!r}) produced a diff that does not "
                    f"apply to the buffer it was handed for {path!r}: {exc}"
                ) from exc
            origin = self._remap(origin, edits, spans)
            claims.extend((rule.id, span) for span in spans)
            fired.append(rule.id)
            expected = len(buffer.splitlines(keepends=True))
            if len(origin) != expected:
                raise EngineContractError(
                    f"rule {rule.id!r}: line bookkeeping desynchronised ({len(origin)} tracked "
                    f"vs {expected} in the buffer) — the diff's hunks do not describe its own text"
                )
        return _PassResult(text=buffer, rule_ids=tuple(fired))

    def _claims_of(
        self, diff: str, origin: Sequence[Span], rule: RewriteRule
    ) -> tuple[list[tuple[int, int, int]], list[Span]]:
        files = parse_unified_diff(diff)
        if len(files) != 1:
            raise EngineContractError(
                f"rule {rule.id!r} produced a {len(files)}-file diff; `Rewriter.apply` rewrites "
                f"exactly the one file it was handed"
            )
        edits: list[tuple[int, int, int]] = []
        for hunk in sorted(files[0].hunks, key=lambda h: h.old_start):
            edits.extend(_edits_of(hunk))
        edits.sort()
        return edits, [_origin_span(origin, start, end) for start, end, _ in edits]

    @staticmethod
    def _remap(
        origin: Sequence[Span], edits: Sequence[tuple[int, int, int]], spans: Sequence[Span]
    ) -> list[Span]:
        """Rebuild the current-line → pass-start-span map after one rule's edits.

        Lines a rule wrote inherit the pass-start span it claimed, which is what makes a later
        rule rewriting that new text collide with the rule that produced it.
        """
        out: list[Span] = []
        cursor = 0
        for (start, end, added), span in zip(edits, spans, strict=True):
            out.extend(origin[cursor:start])
            out.extend([span] * added)
            cursor = end
        out.extend(origin[cursor:])
        return out

    def _conflict_outcome(
        self, path: str, source: str, conflict: _Conflict, passes: int
    ) -> RewriteOutcome:
        finding = RewriteFinding(
            kind="RuleConflict",
            severity="error",
            repo_id=self.repo_id,
            payload={
                "path": path,
                "rule_a": conflict.first,
                "rule_b": conflict.second,
                "span": f"{conflict.span[0]}-{conflict.span[1]}",
            },
        )
        return RewriteOutcome(
            path=path,
            text=source,          # the file is left unchanged
            patch=None,           # ... so there is nothing to apply
            findings=(finding,),
            passes=passes,
            advance_ladder=False,  # a rule defect never spends an escalation rung
        )

    async def _finish(
        self,
        path: str,
        source: str,
        buffer: str,
        applied: tuple[str, ...],
        passes: int,
        findings: tuple[RewriteFinding, ...],
    ) -> RewriteOutcome:
        diff = make_unified_diff(path, source, buffer)
        if not diff:
            return RewriteOutcome(path=path, text=source, passes=passes, findings=findings)
        probe_ok = False if self.probe is None else await self.probe(path, buffer)
        patch = FilePatch(
            path=path,
            diff=diff,
            tier=self.tier,
            parse_probe_ok=probe_ok,
            # One id when exactly one rule fired, else None: `FilePatch.rule_id` is a single
            # attribution and a composed patch has several, which `applied_rule_ids` carries.
            rule_id=applied[0] if len(applied) == 1 else None,
        )
        return RewriteOutcome(
            path=path,
            text=buffer,
            patch=patch,
            findings=findings,
            applied_rule_ids=applied,
            passes=passes,
        )
