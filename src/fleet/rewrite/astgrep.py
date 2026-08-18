"""The `ast-grep` driver — the primary rewrite engine (ADR-0006).

Implements the §7.4 `Rewriter` protocol: `apply` is pure `source` → `FilePatch`. The CLI is a
file-oriented tool, so the driver stages the text in a scratch directory, rewrites *that* copy,
and diffs the two strings it holds; the worktree is never read and never written here — `git
apply` is the only writer (§3.2 step 6.6).

**Absence is an error, not a no-op.** If `ast-grep` is not installed, `apply` raises
`EngineUnavailableError` naming the tool. Returning `None` would be indistinguishable from "the
rule matched nothing", and an unrewritten file would ship as a success.

**An ast-grep exit code answers "did anything match", never "did it parse".** Measured against
0.45.1: `ast-grep run -p '$A'` exits 0 on `const = = ;` (tree-sitter error-recovers and `$A`
matches the wreckage) and exits 1 on a valid empty module (nothing to match). The parse probe
therefore scans for the `ERROR` nodes themselves; see `AstGrepRewriter._probe_document`.

NB on this host: `/usr/bin/sg` is shadow-utils' `newgrp` companion, NOT ast-grep's `sg` alias.
The driver therefore probes for `ast-grep` by name only; pointing `binary` at `sg` on a machine
where that is shadow's binary would run a completely unrelated program.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import yaml  # type: ignore[import-untyped]  # types-PyYAML is not a dependency

from fleet.models.enums import TransformTier
from fleet.models.tasks import FilePatch
from fleet.rewrite.apply import make_unified_diff
from fleet.rewrite.rules import (
    EngineUnavailableError,
    ProbeIndeterminateError,
    RewriteRule,
    language_for_path,
    render_template,
)
from fleet.util.fs import scoped_tempdir
from fleet.util.proc import CommandRunner, run

__all__ = ["REWRITER", "AstGrepRewriter"]

_ERROR_NODES_FOUND: Final = 1
"""ast-grep's exit status when a rule of `severity: error` matched. Distinct from the codes it
uses for its own failures (8 for an unusable rule document), so a broken tool never masquerades
as a broken file."""

_INSTALL_HINT = (
    "install it with `cargo install ast-grep` or `pip install ast-grep-cli`, or point "
    "`transform.engines['ast-grep']` at a driver for a tool this host has"
)


class AstGrepRewriter:
    """`ast-grep scan --inline-rules … --update-all` behind the `Rewriter` protocol."""

    engine = "ast-grep"

    def __init__(
        self,
        *,
        binary: str = "ast-grep",
        runner: CommandRunner = run,
        rules_dir: Path | str | None = None,
        timeout_s: float = 60.0,
        tier: TransformTier = TransformTier.DETERMINISTIC,
    ) -> None:
        self.binary = binary
        self._runner = runner
        self.rules_dir = None if rules_dir is None else Path(rules_dir)
        self.timeout_s = timeout_s
        self.tier = tier

    # -- availability -------------------------------------------------------------------
    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def ensure_available(self) -> None:
        """Rule 11: fail loudly, naming the tool and what to do about it."""
        if not self.available():
            raise EngineUnavailableError(
                f"rewrite engine 'ast-grep' is unavailable: no {self.binary!r} executable on "
                f"PATH — {_INSTALL_HINT}"
            )

    # -- Rewriter -----------------------------------------------------------------------
    async def apply(
        self, rule: RewriteRule, path: str, source: str, params: dict[str, str]
    ) -> FilePatch | None:
        self.ensure_available()
        language = language_for_path(path)
        if language is None or language not in rule.languages:
            return None
        with scoped_tempdir(prefix="fleet-astgrep-") as tmp:
            target = tmp / Path(path).name
            target.write_text(source, encoding="utf-8")
            argv = [self.binary, "scan", "--update-all",
                    *self._rule_args(rule, params, language), str(target)]
            result = await self._runner(argv, cwd=tmp, timeout_s=self.timeout_s)
            if not result.ok:
                raise EngineUnavailableError(
                    f"rule {rule.id!r}: `{' '.join(argv)}` exited {result.exit_code}: "
                    f"{result.stderr_tail.strip()}"
                )
            rewritten = target.read_text(encoding="utf-8")
        diff = make_unified_diff(path, source, rewritten)
        if not diff:
            return None
        return FilePatch(
            path=path, diff=diff, tier=self.tier, parse_probe_ok=False, rule_id=rule.id
        )

    async def parse_probe(self, path: str) -> bool:
        """Does the file on disk still parse under its grammar (§3.2 step 6.2)?

        Answered by scanning for `ERROR` nodes, not by any exit code the tool reports for a
        plain search — see `_probe_document` for why the obvious version of this is wrong.
        """
        self.ensure_available()
        language = language_for_path(path)
        if language is None:
            return True  # no grammar claimed for this suffix; nothing to prove
        return await self._scan_for_error_nodes(Path(path), language, cwd=None)

    async def probe_text(self, path: str, text: str) -> bool:
        """The same question asked of a *buffer*: `pipeline.TextProbe` is `(path, text) -> bool`.

        The pipeline rewrites in memory and has nothing on disk to point `parse_probe` at, and
        the worktree file — if one exists at all — still holds the pre-rewrite text, so probing
        it would answer about the wrong bytes. `text` is staged under `path`'s basename (the
        suffix is what selects the grammar) exactly as `apply` stages its input.
        """
        self.ensure_available()
        language = language_for_path(path)
        if language is None:
            return True
        with scoped_tempdir(prefix="fleet-astgrep-probe-") as tmp:
            target = tmp / Path(path).name
            target.write_text(text, encoding="utf-8")
            return await self._scan_for_error_nodes(target, language, cwd=tmp)

    # -- internals ----------------------------------------------------------------------
    @staticmethod
    def _probe_document(language: str) -> str:
        """The inline rule the probe scans with, and the reasoning behind every part of it.

        **`kind: ERROR`, not a search.** The tempting probe is `ast-grep run --pattern '$A'`,
        and it does not work: tree-sitter *error-recovers* rather than refusing bad input, so
        `const = = ;` still yields a tree — one with `ERROR` nodes in it, which `$A` matches
        just as happily as real code (measured: exit 0, i.e. "parses"). The same exit code runs
        the other way too, since it reports whether anything matched rather than whether a tree
        was built: a valid but empty module matches nothing and exits 1, i.e. "broken". The
        `ERROR` nodes tree-sitter inserts are the actual evidence of a parse failure, so the
        probe looks for them directly.

        **`severity: error`.** A bare scan always exits 0 and the verdict has to be read out of
        `--json`, whose output is truncated to the last `LOG_TAIL_BYTES` by the runner and would
        no longer be parseable JSON for a badly mangled file. Declaring the rule an error makes
        ast-grep exit 1 on a match, which turns the verdict into one integer that cannot be
        truncated.

        `MISSING` is deliberately absent: tree-sitter's other damage marker is not a queryable
        kind in ast-grep 0.45.1 (`kind: MISSING` is rejected outright, exit 8), so an unclosed
        brace that recovers into a MISSING token alone reads as parsing. That is the safe
        direction — a probe that passes an odd file loses a guard, one that fails good files
        quarantines correct rewrites — but it is a real limit, not full parser validation.
        """
        document: str = yaml.safe_dump(
            {
                "id": "fleet-parse-probe",
                "language": language,
                "rule": {"kind": "ERROR"},
                "severity": "error",
            },
            sort_keys=True,
        )
        return document

    async def _scan_for_error_nodes(self, target: Path, language: str, *, cwd: Path | None) -> bool:
        """`False` if ast-grep reported an `ERROR` node, or if `target` does not exist. A tool
        failure is neither verdict, so it raises (Rule 11) rather than resolving to a parse result
        nobody measured."""
        # A target that is not there is not a parse verdict either, and ast-grep will not say so
        # in its exit code: measured against 0.45.1, `scan … missing.ts` prints `ERROR: missing.ts:
        # No such file or directory` on stderr and exits **0**, which the reading below would take
        # for "no ERROR nodes — it parses". `False` rather than raising, even though that is this
        # helper's other habit: every caller of this verdict is a gate, and for a gate the safe
        # direction is refusal — raising here would route a vanished file to a pass just as
        # silently as exit 0 did.
        probe_target = target if target.is_absolute() or cwd is None else cwd / target
        if not probe_target.exists():
            return False
        argv = [self.binary, "scan", "--inline-rules", self._probe_document(language), str(target)]
        result = await self._runner(argv, cwd=cwd, timeout_s=self.timeout_s)
        if result.started and not result.timed_out:
            if result.exit_code == _ERROR_NODES_FOUND:
                return False
            if result.exit_code == 0:
                return True
        # The probe RAN and produced no verdict — killed at its deadline, never started, or an
        # exit code that is neither 0 nor 1 (e.g. 8 for an unusable rule document). This is
        # deliberately `ProbeIndeterminateError`, NOT `EngineUnavailableError` (ADR-0067, D37):
        # `ensure_available()` in `parse_probe`/`probe_text` is the only thing in this driver that
        # reports a missing binary, so by the time this helper runs the tool is known to be
        # present. Reusing `EngineUnavailableError` here would let `cli._transform_criterion`'s
        # `except EngineUnavailableError` arm bucket a corrupt-probe signal into its non-blocking
        # "no rewrite engine is installed" warning — the exact defect this type exists to end.
        raise ProbeIndeterminateError(
            f"parse probe for {target.name!r}: `{self.binary} scan` exited {result.exit_code} "
            f"(started={result.started}, timed_out={result.timed_out}): "
            f"{result.stderr_tail.strip()}"
        )

    def _rule_args(self, rule: RewriteRule, params: Mapping[str, str], language: str) -> list[str]:
        """Either `--rule <file>` from the rules dir, or an interpolated `--inline-rules` doc."""
        if rule.rule_file is not None:
            if self.rules_dir is None:
                raise ValueError(
                    f"rule {rule.id!r} uses `rule_file` but the driver was built without a "
                    f"rules_dir, so {rule.rule_file!r} cannot be resolved"
                )
            resolved = self.rules_dir / rule.rule_file
            if not resolved.is_file():
                raise ValueError(f"rule {rule.id!r}: rule_file {resolved} does not exist")
            return ["--rule", str(resolved)]
        merged = {**rule.params, **params}
        document: dict[str, object] = {"id": rule.id, "language": language, "rule": rule.rule}
        if rule.fix is not None:
            document["fix"] = rule.fix
        # `sort_keys=True` so the same rule renders byte-identically on every host: the inline
        # document lands in the argv a test asserts on and in the log an operator reads.
        text = render_template(yaml.safe_dump(document, sort_keys=True), merged)
        return ["--inline-rules", text]


REWRITER: AstGrepRewriter = AstGrepRewriter()
"""The instance `transform.engines` resolves to (`fleet.rewrite.astgrep`)."""
