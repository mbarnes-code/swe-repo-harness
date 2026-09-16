"""`fleet.rewrite`: the deterministic layer (SPEC §7.4).

The pipeline is tested with an **in-test fake engine**, deliberately and not as a compromise:
`ast-grep`, `libcst` and `ts-morph` are all absent from this host and none of them is a
dependency, so a suite that needed one would be a suite that never ran. What §7.4 actually
specifies — the buffer, the total order, the fixpoint bound, conflict detection — is engine
independent by construction, and the fake proves exactly those.

The real drivers are tested for the one property that matters while their tools are missing:
that absence raises, naming the tool. A driver that returned `None` instead would be
indistinguishable from "the rule matched nothing", and an unrewritten file would ship as success.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from itertools import permutations
from pathlib import Path

import pytest

from fleet.models.enums import TransformTier
from fleet.models.tasks import FilePatch
from fleet.rewrite.apply import (
    ApplyResult,
    apply_in_memory,
    apply_patch,
    check_diff,
    diff_paths,
    make_unified_diff,
    parse_unified_diff,
    validate_diff,
)
from fleet.rewrite.astgrep import AstGrepRewriter
from fleet.rewrite.libcst_py import LibCstRewriter
from fleet.rewrite.pipeline import RewritePipeline, TextProbe
from fleet.rewrite.rules import (
    EngineRegistry,
    EngineUnavailableError,
    ProbeIndeterminateError,
    RewriteRule,
    load_rules,
    render_template,
    rule_matches_path,
)
from fleet.rewrite.tsmorph import TsMorphRewriter
from fleet.settings import ConfigFileError, ConfigValidationError, UnresolvedReferenceError
from fleet.util.proc import ProcResult

SRC = str(Path(__file__).resolve().parents[1] / "src")

SOURCE = "alpha\nbeta\ngamma\ndelta\n"
PATH = "pkg/mod.py"


# ---------------------------------------------------------------------------------------
# the fake engine
# ---------------------------------------------------------------------------------------
class FakeRewriter:
    """A `Rewriter` whose transform is a plain `str -> str` keyed by rule id.

    It records the source text it was handed per call, which is how the composition test proves
    rule B saw rule A's OUTPUT rather than the original file.
    """

    engine = "fake"

    def __init__(self, transforms: Mapping[str, Callable[[str], str]]) -> None:
        self._transforms = dict(transforms)
        self.seen: list[tuple[str, str]] = []

    async def apply(
        self, rule: RewriteRule, path: str, source: str, params: dict[str, str]
    ) -> FilePatch | None:
        self.seen.append((rule.id, source))
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


def _git_init(repo: Path) -> None:
    """A one-commit repo, so `git apply` has a committed state to diff against."""
    for args in (
        ["init", "--initial-branch=main", "."],
        ["config", "user.email", "fleet@example.invalid"],
        ["config", "user.name", "Fleet Test"],
        ["add", "--all"],
        ["commit", "-m", "initial"],
    ):
        subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607 - `git` from PATH, as every other suite does
            cwd=repo,
            check=True,
            capture_output=True,
        )


def _stand_in_for_ast_grep(staged: str) -> None:
    """What the real CLI would do to the staged copy: rewrite it in place."""
    target = Path(staged)
    target.write_text(target.read_text(encoding="utf-8").replace("beta", "BETA"), encoding="utf-8")


def rule(rule_id: str, *, priority: int = 100, engine: str = "fake") -> RewriteRule:
    return RewriteRule(
        id=rule_id,
        engine=engine,
        languages=["python"],
        applies_to=["**/*.py"],
        rule={"pattern": rule_id},
        priority=priority,
    )


def pipeline(
    rules: Sequence[RewriteRule], engine: FakeRewriter, *, max_passes: int = 3
) -> RewritePipeline:
    return RewritePipeline(rules, EngineRegistry([engine]), max_passes=max_passes)


# ---------------------------------------------------------------------------------------
# 1. composition — the headline property the `source:` signature exists for
# ---------------------------------------------------------------------------------------
async def test_two_rules_over_one_file_compose_into_one_patch() -> None:
    """Rule B must see rule A's OUTPUT text, and the file must yield ONE patch.

    Why this is the headline: when `Rewriter.apply` took a path, each of N rules diffed the same
    file against unspecified on-disk state, so two rules produced two diffs against the same
    original — `git apply` accepted the first and REJECTED the second, and the escalation ladder
    then charged a rule-authoring problem to the LLM repair budget. One buffer, one patch, and a
    patch that applies to the committed text is the whole fix.
    """
    engine = FakeRewriter(
        {
            "a-first": lambda t: t.replace("alpha", "ALPHA"),
            "b-second": lambda t: t.replace("gamma", "GAMMA"),
        }
    )
    outcome = await pipeline([rule("a-first"), rule("b-second")], engine).rewrite_file(PATH, SOURCE)

    seen_by_b = next(text for rid, text in engine.seen if rid == "b-second")
    assert "ALPHA" in seen_by_b, "rule B was handed the original file, not rule A's output"

    assert outcome.patch is not None
    assert outcome.applied_rule_ids == ("a-first", "b-second")
    assert len(parse_unified_diff(outcome.patch.diff)) == 1, "one patch per file, not two"
    # The single patch applies to the file's COMMITTED text — the property `git apply` needs.
    assert apply_in_memory(SOURCE, outcome.patch.diff) == "ALPHA\nbeta\nGAMMA\ndelta\n"
    assert outcome.patch.rule_id is None, "a composed patch has no single rule to attribute it to"
    assert outcome.advance_ladder is True


async def test_composed_patch_is_one_diff_not_two_conflicting_ones() -> None:
    """Adjacent-line rewrites by two rules must not produce two diffs against the same original.

    Two independent diffs both anchored at line 2 are precisely what `git apply` rejects; the
    composed patch has to contain both edits and apply in one call.
    """
    engine = FakeRewriter(
        {
            "r1": lambda t: t.replace("beta", "BETA"),
            "r2": lambda t: t.replace("gamma", "GAMMA"),
        }
    )
    outcome = await pipeline([rule("r1"), rule("r2")], engine).rewrite_file(PATH, SOURCE)
    assert outcome.patch is not None
    assert "BETA" in outcome.patch.diff and "GAMMA" in outcome.patch.diff
    assert apply_in_memory(SOURCE, outcome.patch.diff) == "alpha\nBETA\nGAMMA\ndelta\n"


# ---------------------------------------------------------------------------------------
# 2. conflict — finding, unchanged file, ladder frozen
# ---------------------------------------------------------------------------------------
async def test_overlapping_rules_conflict_leave_file_unchanged_and_ladder_unmoved() -> None:
    """Two rules rewriting the same span is a YAML defect, and the harness must say so.

    All three consequences are asserted together because dropping any one of them is a distinct
    production failure: no finding hides the defect, a mutated file half-applies it, and an
    advanced ladder spends two escalation rungs and a HEAVY call for an LLM to rediscover that
    two rules disagree.
    """
    engine = FakeRewriter(
        {
            "left": lambda t: t.replace("beta", "beta-left"),
            "right": lambda t: t.replace("beta", "beta-right"),
        }
    )
    outcome = await pipeline([rule("left"), rule("right")], engine).rewrite_file(PATH, SOURCE)

    assert outcome.conflicted
    (finding,) = outcome.findings
    assert finding.kind == "RuleConflict"
    assert finding.severity == "error"
    assert {finding.payload["rule_a"], finding.payload["rule_b"]} == {"left", "right"}
    assert finding.payload["span"] == "1-2"  # 0-based half-open: the `beta` line
    assert outcome.text == SOURCE, "the file is left unchanged"
    assert outcome.patch is None, "nothing to apply"
    assert outcome.advance_ladder is False, "a rule defect must not spend an LLM rung"


async def test_rule_rewriting_another_rules_output_in_the_same_pass_is_a_conflict() -> None:
    """§13 row 46's other half: rule B rewrites the very text rule A just produced.

    The claim map follows text through the pass, so B's edit resolves back to A's span rather
    than looking like an independent edit on a line nobody owns.
    """
    engine = FakeRewriter(
        {
            "a": lambda t: t.replace("beta", "MIDDLE"),
            "b": lambda t: t.replace("MIDDLE", "FINAL"),
        }
    )
    outcome = await pipeline([rule("a"), rule("b")], engine).rewrite_file(PATH, SOURCE)
    assert outcome.conflicted
    assert outcome.text == SOURCE
    assert outcome.advance_ladder is False


async def test_disjoint_insertions_do_not_conflict() -> None:
    """Conflict detection must not fire on rules that merely touch the same FILE.

    Hunks carry three context lines either side; claiming a whole hunk instead of its changed
    runs would fabricate a `RuleConflict` between two perfectly composable rewrites.
    """
    engine = FakeRewriter(
        {
            # Idempotent by construction: a rule that re-fires on its own output is the
            # oscillation case, which `test_oscillating_rule_...` covers separately.
            "top": lambda t: t if "ALPHA2" in t else t.replace("alpha\n", "alpha\nALPHA2\n"),
            "bottom": lambda t: t if "DELTA2" in t else t.replace("delta\n", "delta\nDELTA2\n"),
        }
    )
    outcome = await pipeline([rule("top"), rule("bottom")], engine).rewrite_file(PATH, SOURCE)
    assert not outcome.conflicted
    assert outcome.text == "alpha\nALPHA2\nbeta\ngamma\ndelta\nDELTA2\n"


# ---------------------------------------------------------------------------------------
# 3. fixpoint
# ---------------------------------------------------------------------------------------
def _toggle(text: str) -> str:
    return text.replace("beta", "BETA") if "beta" in text else text.replace("BETA", "beta")


async def test_oscillating_rule_terminates_at_max_passes_with_a_finding() -> None:
    """An oscillating rule set must be bounded, visible, and must still ship a whole buffer.

    Unbounded is the real hazard: a rule pair that undoes each other would spin a worker forever
    inside one task, holding a worktree and a slot, with no finding to point at the YAML. The
    ceiling converts that into three passes and one row.
    """
    engine = FakeRewriter({"toggle": _toggle})
    outcome = await pipeline([rule("toggle")], engine, max_passes=3).rewrite_file(PATH, SOURCE)

    assert outcome.oscillated
    (finding,) = outcome.findings
    assert finding.kind == "RuleOscillation"
    assert finding.payload["max_passes"] == "3"
    assert outcome.passes == 3
    # Pass 3 is a COMPLETE application of the rule set; a half-applied buffer never ships.
    assert outcome.text == "alpha\nBETA\ngamma\ndelta\n"
    assert outcome.patch is not None


async def test_fixpoint_stops_as_soon_as_the_buffer_stops_changing() -> None:
    """A converging rule set costs one confirming pass, not `max_passes` of them."""
    engine = FakeRewriter({"once": lambda t: t.replace("beta", "BETA")})
    outcome = await pipeline([rule("once")], engine, max_passes=5).rewrite_file(PATH, SOURCE)
    assert outcome.passes == 2  # pass 1 changed it, pass 2 confirmed the fixpoint
    assert not outcome.oscillated
    assert outcome.text == "alpha\nBETA\ngamma\ndelta\n"


async def test_a_cascade_across_passes_is_allowed_and_converges() -> None:
    """Rule B may legitimately act on rule A's output on the NEXT pass — that is what the
    fixpoint loop is for, and it must not be mistaken for oscillation."""
    engine = FakeRewriter(
        {
            "a": lambda t: t.replace("beta", "MIDDLE"),
            "z": lambda t: t.replace("MIDDLE\ngamma", "MIDDLE\nGAMMA"),
        }
    )
    outcome = await pipeline([rule("a"), rule("z")], engine).rewrite_file(PATH, SOURCE)
    assert not outcome.conflicted and not outcome.oscillated
    assert outcome.text == "alpha\nMIDDLE\nGAMMA\ndelta\n"


# ---------------------------------------------------------------------------------------
# 4. ordering
# ---------------------------------------------------------------------------------------
async def test_order_is_priority_then_id_under_every_registration_order() -> None:
    """`(priority, id)` is TOTAL: equal-priority rules may not depend on registration order.

    Rules arrive from `rglob` over a rules directory, so "whatever order they were registered in"
    means "whatever order this host's filesystem enumerated", and the same fleet would compose
    two different patches on two machines.
    """
    ids = ["b-rule", "a-rule", "c-rule"]
    expected = ["low-priority", "a-rule", "b-rule", "c-rule"]
    for order in permutations([*ids, "low-priority"]):
        rules = [rule(rid, priority=10 if rid == "low-priority" else 100) for rid in order]
        engine = FakeRewriter({})  # every rule is a no-op; only the call order is under test
        await pipeline(rules, engine).rewrite_file(PATH, SOURCE)
        assert [rid for rid, _ in engine.seen] == expected


# ---------------------------------------------------------------------------------------
# 5. engine resolution is a startup error
# ---------------------------------------------------------------------------------------
def test_unresolvable_engine_is_a_startup_error_naming_the_engine() -> None:
    """§7.4: unresolvable is a STARTUP error, exactly as an unknown `backend` is.

    Resolving lazily turns a one-character YAML typo into a wave-7 `KeyError` after hours of
    spend, on the first file that happens to match the rule.
    """
    with pytest.raises(UnresolvedReferenceError) as excinfo:
        RewritePipeline([rule("typo", engine="ast-grepp")], EngineRegistry([FakeRewriter({})]))
    message = str(excinfo.value)
    assert "ast-grepp" in message and "typo" in message


def test_engine_registry_resolves_the_shipped_engines_from_config() -> None:
    """The §9 `transform.engines` map must actually import — the map is the open set, and a
    module that is not importable is the same defect class as a misspelled engine name."""
    registry = EngineRegistry.from_modules(
        {
            "ast-grep": "fleet.rewrite.astgrep",
            "libcst": "fleet.rewrite.libcst_py",
            "ts-morph": "fleet.rewrite.tsmorph",
        }
    )
    assert registry.names == ("ast-grep", "libcst", "ts-morph")
    with pytest.raises(UnresolvedReferenceError, match=r"fleet\.rewrite\.nope"):
        EngineRegistry.from_modules({"nope": "fleet.rewrite.nope"})


# ---------------------------------------------------------------------------------------
# 5b. an engine that violates its contract is loud, never absorbed into a finding
# ---------------------------------------------------------------------------------------
async def test_engine_contract_violation_for_the_wrong_path_is_never_absorbed() -> None:
    """`pipeline.py`'s module docstring draws a hard line: a genuine rule disagreement is a
    `RuleConflict` finding (an operator's YAML problem), while a driver returning a patch for a
    path it was not handed is `EngineContractError` (Rule 11 — ours, not theirs) and must
    propagate rather than being swallowed or misfiled as a finding. `EngineContractError` had
    zero coverage anywhere in this suite before this test — nothing proved `_run_pass`'s
    `patch.path != path` guard (`pipeline.py`) actually fires."""

    class WrongPathRewriter:
        engine = "fake"

        async def apply(
            self, rule: RewriteRule, path: str, source: str, params: dict[str, str]
        ) -> FilePatch | None:
            wrong_path = "pkg/other.py"
            diff = make_unified_diff(wrong_path, source, source.replace("beta", "BETA"))
            return FilePatch(
                path=wrong_path,
                diff=diff,
                tier=TransformTier.DETERMINISTIC,
                parse_probe_ok=False,
                rule_id=rule.id,
            )

        async def parse_probe(self, path: str) -> bool:
            return True

    from fleet.rewrite.pipeline import EngineContractError

    with pytest.raises(EngineContractError) as excinfo:
        await RewritePipeline([rule("r")], EngineRegistry([WrongPathRewriter()])).rewrite_file(
            PATH, SOURCE
        )
    assert "pkg/other.py" in str(excinfo.value)


# ---------------------------------------------------------------------------------------
# 6. determinism across processes
# ---------------------------------------------------------------------------------------
_DETERMINISM_SCRIPT = """
import asyncio, json, sys
from fleet.models.enums import TransformTier
from fleet.models.tasks import FilePatch
from fleet.rewrite.apply import make_unified_diff
from fleet.rewrite.pipeline import RewritePipeline
from fleet.rewrite.rules import EngineRegistry, RewriteRule

SOURCE = "alpha\\nbeta\\ngamma\\ndelta\\n"
TRANSFORMS = {
    "r-alpha": lambda t: t.replace("alpha", "ALPHA"),
    "r-gamma": lambda t: t.replace("gamma", "GAMMA"),
    "r-delta": lambda t: t.replace("delta", "DELTA"),
}

class Fake:
    engine = "fake"
    async def apply(self, rule, path, source, params):
        new = TRANSFORMS[rule.id](source)
        diff = make_unified_diff(path, source, new)
        if not diff:
            return None
        return FilePatch(path=path, diff=diff, tier=TransformTier.DETERMINISTIC,
                         parse_probe_ok=False, rule_id=rule.id)
    async def parse_probe(self, path):
        return True

def rule(rid):
    return RewriteRule(id=rid, engine="fake", languages=["python"], applies_to=["**/*.py"],
                       rule={"pattern": rid}, priority=100)

rules = [rule(r) for r in ("r-gamma", "r-delta", "r-alpha")]
pipe = RewritePipeline(rules, EngineRegistry([Fake()]))
out = asyncio.run(pipe.rewrite_files({"pkg/b.py": SOURCE, "pkg/a.py": SOURCE}))
print(json.dumps([[o.path, o.patch.diff, list(o.applied_rule_ids)] for o in out]))
"""


def _run_pipeline_in_subprocess(script: Path, hashseed: str) -> str:
    """A real subprocess, because `PYTHONHASHSEED` is fixed at interpreter start."""
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONHASHSEED": hashseed, "PYTHONPATH": SRC, "PATH": os.environ.get("PATH", "")},
    )
    return proc.stdout


def test_patches_are_identical_across_processes_and_hash_seeds(tmp_path: Path) -> None:
    """Same inputs → same patches in the same order, in a fresh interpreter.

    Set iteration and dict ordering are the classic ways a "deterministic" layer stops being one;
    §11.6 makes reproducibility a promise, and a patch that differs run to run makes `fleet
    resume` report drift on work that never changed.
    """
    script = tmp_path / "determinism.py"
    script.write_text(_DETERMINISM_SCRIPT, encoding="utf-8")
    outputs = {_run_pipeline_in_subprocess(script, seed) for seed in ("0", "1", "12345")}
    assert len(outputs) == 1, "the pipeline's output depends on PYTHONHASHSEED"
    payload = json.loads(outputs.pop())
    assert [row[0] for row in payload] == ["pkg/a.py", "pkg/b.py"]  # sorted, not insertion order
    assert payload[0][2] == ["r-alpha", "r-delta", "r-gamma"]  # (priority, id)


# ---------------------------------------------------------------------------------------
# 7. real drivers: absence is loud, not a silent no-op
# ---------------------------------------------------------------------------------------
async def test_astgrep_driver_names_the_missing_tool() -> None:
    """A missing `ast-grep` must raise, not return `None`.

    `None` is the protocol's spelling of "this rule matched nothing". Reporting an absent engine
    that way makes an unrewritten file indistinguishable from a clean no-op, and the repo ships
    unmigrated with a green build.
    """
    driver = AstGrepRewriter(binary="fleet-no-such-ast-grep")
    with pytest.raises(EngineUnavailableError) as excinfo:
        await driver.apply(rule("r", engine="ast-grep"), PATH, SOURCE, {})
    message = str(excinfo.value)
    assert "ast-grep" in message
    assert "fleet-no-such-ast-grep" in message and "PATH" in message


async def test_libcst_driver_names_the_missing_package() -> None:
    """Same contract for the fenced Python engine."""
    if importlib.util.find_spec("libcst") is not None:
        pytest.skip("libcst IS installed here; the absence path cannot be exercised")
    with pytest.raises(EngineUnavailableError, match="libcst"):
        await LibCstRewriter().apply(
            RewriteRule(id="r", engine="libcst", languages=["python"], rule={"pattern": "x"}),
            PATH,
            SOURCE,
            {},
        )


async def test_libcst_parse_probe_is_a_real_parse_not_gated_by_availability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The module docstring draws a sharp line: `apply` is unimplemented, but `parse_probe` is
    "real: `libcst.parse_module` on the file's current bytes." With `libcst` absent from this
    host, that real True/False verdict logic (`parse_probe`'s try/except) has never run under
    this suite — only the availability gate has (the test above). `importlib.util.find_spec` and
    `importlib.import_module` are patched to simulate a host where `libcst` resolves, so the
    genuine parse/parse-failure branches are exercised without depending on the package being
    installed."""
    import importlib as real_importlib

    real_find_spec = real_importlib.util.find_spec
    real_import_module = real_importlib.import_module

    class _FakeLibcst:
        @staticmethod
        def parse_module(text: str) -> object:
            if "BROKEN" in text:
                raise SyntaxError("bad syntax")
            return object()

    def fake_find_spec(name: str, package: str | None = None) -> object:
        return object() if name == "libcst" else real_find_spec(name, package)

    def fake_import_module(name: str, package: str | None = None) -> object:
        return _FakeLibcst() if name == "libcst" else real_import_module(name, package)

    monkeypatch.setattr(real_importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(real_importlib, "import_module", fake_import_module)

    driver = LibCstRewriter()
    assert driver.available() is True

    good = tmp_path / "good.py"
    good.write_text("x = 1\n", encoding="utf-8")
    bad = tmp_path / "bad.py"
    bad.write_text("BROKEN(\n", encoding="utf-8")

    assert await driver.parse_probe(str(good)) is True
    assert await driver.parse_probe(str(bad)) is False


async def test_tsmorph_driver_names_the_missing_half() -> None:
    """Node present but `ts-morph` unresolvable is the common host state, and it must still fail
    loudly: probing only for `node` would let the rule "run" and rewrite nothing."""
    driver = TsMorphRewriter()
    ts_rule = RewriteRule(id="r", engine="ts-morph", languages=["tsx"], rule={"pattern": "x"})
    if shutil.which("node") is not None and await driver.available():
        pytest.skip("ts-morph IS resolvable here; the absence path cannot be exercised")
    with pytest.raises(EngineUnavailableError) as excinfo:
        await driver.apply(ts_rule, "web/App.tsx", "export const A = 1;\n", {})
    message = str(excinfo.value)
    assert "ts-morph" in message
    assert "node" in message or "npm install" in message


async def test_tsmorph_apply_and_probe_raise_notimplemented_once_available() -> None:
    """State-of-implementation contract (Rule 11), for the OTHER half of the driver's behavior
    from the test above: once both availability halves resolve — `node` on PATH and `ts-morph`
    resolvable from it — `apply`/`parse_probe` must still fail LOUDLY with `NotImplementedError`,
    never silently no-op, since the bridge script itself is not shipped. This host has `node` but
    not `ts-morph`, so the injected `CommandRunner` simulates the `require.resolve` half
    succeeding — the branch nothing in this suite reaches otherwise."""

    async def resolving_runner(argv: Sequence[str], **kwargs: object) -> ProcResult:
        return ProcResult(
            argv=tuple(argv),
            exit_code=0,
            stdout_tail="",
            stderr_tail="",
            duration_ms=1,
            timed_out=False,
            started=True,
        )

    driver = TsMorphRewriter(runner=resolving_runner)  # type: ignore[arg-type]
    assert await driver.available() is True

    ts_rule = RewriteRule(id="r", engine="ts-morph", languages=["tsx"], rule={"pattern": "x"})
    with pytest.raises(NotImplementedError, match="bridge script"):
        await driver.apply(ts_rule, "web/App.tsx", "export const A = 1;\n", {})
    with pytest.raises(NotImplementedError, match="bridge script"):
        await driver.parse_probe("web/App.tsx")


async def test_fenced_engines_refuse_a_rule_outside_their_language() -> None:
    """The ADR-0006 fence is enforced by the driver, not trusted to rule authors: a `libcst` rule
    claiming Java would otherwise be a Python parser aimed at a Java file."""
    with pytest.raises(ValueError, match="fence"):
        await LibCstRewriter().apply(
            RewriteRule(id="r", engine="libcst", languages=["java"], rule={"pattern": "x"}),
            "Main.java",
            "class Main {}\n",
            {},
        )


async def test_astgrep_builds_a_deterministic_inline_rule_invocation() -> None:
    """The driver's plumbing — stage, invoke, diff — is assertable with an injected runner even
    though the tool is absent, and the inline rule document must be byte-stable across hosts."""
    calls: list[tuple[str, ...]] = []

    async def fake_runner(argv: Sequence[str], **kwargs: object) -> ProcResult:
        calls.append(tuple(argv))
        _stand_in_for_ast_grep(argv[-1])
        return ProcResult(
            argv=tuple(argv),
            exit_code=0,
            stdout_tail="",
            stderr_tail="",
            duration_ms=1,
            timed_out=False,
        )

    driver = AstGrepRewriter(binary=sys.executable, runner=fake_runner)  # type: ignore[arg-type]
    target_rule = RewriteRule(
        id="pkg-rename",
        engine="ast-grep",
        languages=["python"],
        rule={"pattern": "{{old_pkg}}"},
        fix="{{new_pkg}}",
    )
    patch = await driver.apply(target_rule, PATH, SOURCE, {"old_pkg": "a.b", "new_pkg": "c.d"})
    assert patch is not None and "BETA" in patch.diff
    inline = calls[0][calls[0].index("--inline-rules") + 1]
    assert "a.b" in inline and "c.d" in inline and "{{" not in inline
    assert inline.index("fix") < inline.index("id") < inline.index("language")  # sorted keys


async def test_astgrep_apply_raises_when_the_cli_itself_reports_failure() -> None:
    """The CLI wrapper's own failure path (Rule 11), never exercised by the tests above since
    every injected runner there returns exit 0: `ast-grep scan --update-all` exiting non-zero —
    e.g. `8`, ast-grep's own "this rule document is unusable" code — must raise
    `EngineUnavailableError` naming the rule, the exit code and stderr. Absorbing this into
    "nothing matched" (a `None` return) would ship the unrewritten file as a silent success,
    exactly the failure `apply`'s module docstring warns against."""

    async def failing_runner(argv: Sequence[str], **kwargs: object) -> ProcResult:
        return ProcResult(
            argv=tuple(argv),
            exit_code=8,
            stdout_tail="",
            stderr_tail="Error: rule has no valid `rule` field",
            duration_ms=5,
            timed_out=False,
            started=True,
        )

    driver = AstGrepRewriter(binary=sys.executable, runner=failing_runner)  # type: ignore[arg-type]
    with pytest.raises(EngineUnavailableError) as excinfo:
        await driver.apply(rule("busted", engine="ast-grep"), PATH, SOURCE, {})
    message = str(excinfo.value)
    assert "busted" in message
    assert "exited 8" in message
    assert "no valid" in message


# ---------------------------------------------------------------------------------------
# 7b. the ast-grep parse probe, against the REAL binary
# ---------------------------------------------------------------------------------------
# These four run the vendored `tools/bin/ast-grep` (conftest puts it on PATH) on purpose and
# carry NO skip guard: the probe is the gate §3.2 step 6.2 commits on, and a probe that silently
# stopped running would let corrupted files through with a green suite. If the binary is gone
# these tests must go red, not yellow.
#
# What is being pinned is the *verdict*, never an exit code. `ast-grep run -p '$A'` exits on
# "did anything match", which is a different question: tree-sitter error-recovers, so garbage
# parses into ERROR nodes that `$A` happily matches (exit 0), while a valid file with nothing
# to match — an empty module — exits 1.


def _staged(tmp_path: Path, name: str, text: str) -> str:
    target = tmp_path / name
    target.write_text(text, encoding="utf-8")
    return str(target)


def _constant(text: str) -> Callable[[str], str]:
    """A `FakeRewriter` transform that replaces the whole buffer, whatever it held."""
    return lambda _source: text


BROKEN_TS = "const = = ;\n"
BROKEN_PY = "def f(:\n    return ???\n"


async def test_astgrep_parse_probe_rejects_typescript_that_does_not_parse(
    tmp_path: Path,
) -> None:
    """The defect this pins: tree-sitter recovers from garbage into `ERROR` nodes rather than
    refusing it, so an exit-code probe calls a wrecked file healthy and the rewrite ships."""
    driver = AstGrepRewriter()
    assert await driver.parse_probe(_staged(tmp_path, "bad.ts", BROKEN_TS)) is False
    assert (
        await driver.parse_probe(_staged(tmp_path, "half.ts", "function f( {\n  const y = ;\n"))
        is False
    )


async def test_astgrep_parse_probe_accepts_valid_typescript_with_nothing_to_match(
    tmp_path: Path,
) -> None:
    """The other half of the same defect, and the more dangerous one: an empty or comment-only
    module is *valid*, and a probe that failed it would quarantine correct rewrites."""
    driver = AstGrepRewriter()
    assert await driver.parse_probe(_staged(tmp_path, "ok.ts", "export const x: number = 1;\n"))
    assert await driver.parse_probe(_staged(tmp_path, "empty.ts", ""))
    assert await driver.parse_probe(_staged(tmp_path, "note.ts", "// just a comment\n"))


async def test_astgrep_parse_probe_holds_for_python_and_unknown_suffixes(
    tmp_path: Path,
) -> None:
    """The verdict must not be a TypeScript special case — `.py` maps to a real grammar too — and
    a suffix with no grammar claimed still passes, since nothing was proven wrong about it."""
    driver = AstGrepRewriter()
    assert await driver.parse_probe(_staged(tmp_path, "bad.py", BROKEN_PY)) is False
    assert await driver.parse_probe(_staged(tmp_path, "ok.py", "def f(x):\n    return x + 1\n"))
    assert await driver.parse_probe(_staged(tmp_path, "empty.py", ""))
    assert await driver.parse_probe(_staged(tmp_path, "notes.txt", BROKEN_TS))


async def test_astgrep_parse_probe_never_passes_a_path_that_is_not_there(tmp_path: Path) -> None:
    """The defect this pins: `ast-grep scan … missing.ts` prints `ERROR: … No such file or
    directory` and exits **0**, so an exit-code reading calls a file that does not exist "parses
    fine" — the probe returns `True` and a safety gate aimed at it passes without probing
    anything. A verdict about bytes nobody read is never `True`."""
    driver = AstGrepRewriter()
    assert await driver.parse_probe(str(tmp_path / "gone.ts")) is False
    assert await driver.parse_probe("no/such/tree/app.ts") is False
    assert await driver.parse_probe(str(tmp_path / "gone.py")) is False
    # A suffix with no grammar claimed still short-circuits before any of this, as it always did.
    assert await driver.parse_probe(str(tmp_path / "gone.txt")) is True


async def test_astgrep_probe_indeterminate_is_not_engine_unavailable(tmp_path: Path) -> None:
    """ADR-0067 (D37): a probe that RAN but produced no verdict — killed at its deadline, never
    started, or an exit code that is neither 0 nor 1 — must raise `ProbeIndeterminateError`, and
    that type must NOT be catchable as `EngineUnavailableError`. Subclassing would let
    `cli._transform_criterion`'s `except EngineUnavailableError` arm keep bucketing an
    indeterminate probe into the non-blocking "no rewrite engine is installed" warning — the same
    defect the new type exists to end. An injected runner is used because the vendored binary
    always produces a real verdict; these three shapes have to be forced.
    """
    assert not issubclass(ProbeIndeterminateError, EngineUnavailableError)
    assert issubclass(ProbeIndeterminateError, RuntimeError)

    cases: list[ProcResult] = [
        # Killed at its deadline: SIGTERM honoured.
        ProcResult(
            argv=("ast-grep",),
            exit_code=-15,
            stdout_tail="",
            stderr_tail="",
            duration_ms=60_000,
            timed_out=True,
            started=True,
        ),
        # Killed at its deadline: SIGTERM ignored, escalated to SIGKILL.
        ProcResult(
            argv=("ast-grep",),
            exit_code=-9,
            stdout_tail="",
            stderr_tail="",
            duration_ms=60_000,
            timed_out=True,
            started=True,
        ),
        # Never started: the deadline had already passed before the call.
        ProcResult(
            argv=("ast-grep",),
            exit_code=124,
            stdout_tail="",
            stderr_tail="",
            duration_ms=0,
            timed_out=True,
            started=False,
        ),
        # Ran, but exited a code that is neither the pass (0) nor the fail (1) verdict — e.g.
        # ast-grep's own "this rule document is unusable" code.
        ProcResult(
            argv=("ast-grep",),
            exit_code=8,
            stdout_tail="",
            stderr_tail="",
            duration_ms=5,
            timed_out=False,
            started=True,
        ),
    ]
    for scripted in cases:

        async def fake_runner(
            argv: Sequence[str], *, _result: ProcResult = scripted, **kwargs: object
        ) -> ProcResult:
            return _result

        driver = AstGrepRewriter(binary=sys.executable, runner=fake_runner)
        target = _staged(tmp_path, "app.ts", "export const x = 1;\n")
        with pytest.raises(ProbeIndeterminateError) as excinfo:
            await driver.parse_probe(target)
        assert "app.ts" in str(excinfo.value)

        # And the existing bucket must NOT catch it — the whole point of the new type.
        try:
            await driver.parse_probe(target)
        except EngineUnavailableError:
            pytest.fail("ProbeIndeterminateError was caught as EngineUnavailableError")
        except ProbeIndeterminateError:
            pass


def test_probe_indeterminate_error_is_exported_from_the_package() -> None:
    """ADR-0067 part 1: `ProbeIndeterminateError` must be reachable as `fleet.rewrite`, exactly
    like its sibling `EngineUnavailableError` — a caller outside `rewrite/rules.py` should never
    need the submodule path to catch it."""
    import fleet.rewrite as rewrite_pkg

    assert rewrite_pkg.ProbeIndeterminateError is ProbeIndeterminateError
    assert "ProbeIndeterminateError" in rewrite_pkg.__all__


async def test_astgrep_probe_still_names_a_genuinely_missing_binary_as_engine_unavailable(
    tmp_path: Path,
) -> None:
    """The other half of the split: a binary that is not on PATH at all is unaffected by this
    change — `ensure_available()` still raises `EngineUnavailableError`, before the probe helper
    that can raise `ProbeIndeterminateError` is ever reached."""
    driver = AstGrepRewriter(binary="fleet-no-such-ast-grep")
    with pytest.raises(EngineUnavailableError):
        await driver.parse_probe(_staged(tmp_path, "app.ts", "export const x = 1;\n"))


async def test_astgrep_probe_text_judges_the_buffer_the_pipeline_holds(tmp_path: Path) -> None:
    """`pipeline.TextProbe` is `(path, text) -> parses?`: the pipeline rewrites in memory and has
    no file to point `parse_probe` at. `probe_text` must judge the *text*, so the on-disk file
    here is deliberately valid while the buffer handed in is not."""
    driver = AstGrepRewriter()
    on_disk = _staged(tmp_path, "mod.ts", "export const x: number = 1;\n")
    assert await driver.probe_text(on_disk, BROKEN_TS) is False
    assert await driver.probe_text("web/App.ts", "export const y = 2;\n") is True
    assert await driver.probe_text("web/App.ts", "") is True  # valid, matches nothing
    assert await driver.probe_text("pkg/mod.py", BROKEN_PY) is False
    # A path that does not exist is fine: the text is staged, never read from the worktree.
    assert await driver.probe_text("no/such/file.ts", BROKEN_TS) is False


async def test_the_pipeline_accepts_probe_text_as_its_injected_text_probe() -> None:
    """`probe_text` exists to be *used*, so this pins the seam rather than the method: the real
    driver's bound method is passed as `RewritePipeline(probe=...)` and the verdict it produces
    is what lands in `FilePatch.parse_probe_ok`, for a rewrite that wrecks the file and one
    that does not."""
    probe: TextProbe = AstGrepRewriter().probe_text
    py_rule = RewriteRule(
        id="wreck",
        engine="fake",
        languages=["python"],
        applies_to=["**/*.py"],
        rule={"pattern": "wreck"},
    )
    valid, broken = "def f(x):\n    return x\n", "def f(:\n    return ???\n"
    for rewritten, expected in ((valid.replace("return x", "return x + 1"), True), (broken, False)):
        engine = FakeRewriter({"wreck": _constant(rewritten)})
        outcomes = await RewritePipeline(
            [py_rule], EngineRegistry([engine]), probe=probe
        ).rewrite_files({"pkg/mod.py": valid})
        assert outcomes[0].patch is not None
        assert outcomes[0].patch.parse_probe_ok is expected


# ---------------------------------------------------------------------------------------
# 7c. the ast-grep REWRITE itself, against the REAL binary: bytes in, bytes out
# ---------------------------------------------------------------------------------------
# 7b pins the *probe*; everything above it pins the pipeline against a fake engine. Neither
# proves the thing the whole deterministic layer is for: that `ast-grep` actually rewrote the
# file. These tests run the vendored `tools/bin/ast-grep` (conftest puts it on PATH) with NO skip
# guard — a missing binary must go red, because a rewrite engine that quietly stopped rewriting
# is exactly the failure that ships 250 unmigrated repos with a green suite.
#
# The assertions are on FULL POST-IMAGE TEXT, never on a predicate the un-rewritten source also
# satisfies ("a patch was produced", "`logger` appears somewhere"). `apply_in_memory` reconstructs
# the post-image from the driver's own diff, so what is compared is the bytes `git apply` will
# later write.

TS_PATH = "web/app.ts"
PY_PATH = "pkg/run.py"

REAL_TS_SOURCE = (
    'import { collect } from "./collect";\n'
    "\n"
    "export function run(x: number): number {\n"
    "  console.log(x);\n"
    "  const y = collect(x);\n"
    "  return y;\n"
    "}\n"
)
REAL_TS_REWRITTEN = (
    'import { collect } from "./collect";\n'
    "\n"
    "export function run(x: number): number {\n"
    "  logger.info(x);\n"
    "  const y = collect(x);\n"
    "  return y;\n"
    "}\n"
)

MULTI_TS_SOURCE = (
    "export function trace(a: number, b: number): number {\n"
    "  console.log(a);\n"
    "  const sum = a + b;\n"
    "  console.log(b);\n"
    "  console.log(sum);\n"
    "  return sum;\n"
    "}\n"
)
MULTI_TS_REWRITTEN = (
    "export function trace(a: number, b: number): number {\n"
    "  logger.info(a);\n"
    "  const sum = a + b;\n"
    "  logger.info(b);\n"
    "  logger.info(sum);\n"
    "  return sum;\n"
    "}\n"
)

REAL_PY_SOURCE = (
    "import collect\n"
    "\n"
    "\n"
    "def run(x):\n"
    "    print(x)\n"
    "    y = collect.gather(x)\n"
    "    print(y)\n"
    "    return y\n"
)
REAL_PY_REWRITTEN = (
    "import collect\n"
    "\n"
    "\n"
    "def run(x):\n"
    "    logger.info(x)\n"
    "    y = collect.gather(x)\n"
    "    logger.info(y)\n"
    "    return y\n"
)

# What a `fix` that emits unbalanced TypeScript does to `console.log(x);` — the statement's own
# semicolon survives, so the wreckage is `const = = ;;`. Measured against the real 0.45.1 binary,
# and `test_astgrep_probe_gate_blocks_the_landing_and_no_commit_is_created` re-measures it.
WRECKED_TS_SOURCE = "export function run(x: number): void {\n  console.log(x);\n}\n"
WRECKED_TS_REWRITTEN = "export function run(x: number): void {\n  const = = ;;\n}\n"


def real_rule(
    rule_id: str, *, language: str, pattern: str, fix: str, priority: int = 100
) -> RewriteRule:
    """A rule the REAL binary can execute — unlike the module-level `rule()`, which is a
    fake-engine fixture whose `pattern` is its own id and which carries no `fix`."""
    return RewriteRule(
        id=rule_id,
        engine="ast-grep",
        languages=[language],
        applies_to=["**/*"],
        rule={"pattern": pattern},
        fix=fix,
        priority=priority,
    )


def ts_console_rule(
    rule_id: str = "console-to-logger", *, fix: str = "logger.info($A)"
) -> RewriteRule:
    return real_rule(rule_id, language="typescript", pattern="console.log($A)", fix=fix)


def _repo_with(tmp_path: Path, rel_path: str, text: str) -> Path:
    """A one-commit repo holding exactly `rel_path`, so `git apply` has committed text to hit."""
    repo = tmp_path / "repo"
    (repo / rel_path).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel_path).write_text(text, encoding="utf-8")
    _git_init(repo)
    return repo


def _git_out(repo: Path, *args: str) -> str:
    proc = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607 - `git` from PATH, as every other suite does
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


async def test_astgrep_rewrites_real_typescript_to_exact_bytes(tmp_path: Path) -> None:
    """Intent: the real binary must turn real source into EXACTLY the expected post-image bytes —
    the claim "the rewrite engine rewrites" is a byte equality or it is nothing."""
    patch = await AstGrepRewriter().apply(ts_console_rule(), TS_PATH, REAL_TS_SOURCE, {})

    assert patch is not None, "the real ast-grep produced no patch for a source it matches"
    assert patch.path == TS_PATH
    assert patch.rule_id == "console-to-logger"
    assert patch.tier is TransformTier.DETERMINISTIC
    assert patch.parse_probe_ok is False, "the driver never claims a probe it did not run"

    # The bytes `git apply` would write, reconstructed from the driver's OWN diff.
    assert apply_in_memory(REAL_TS_SOURCE, patch.diff) == REAL_TS_REWRITTEN
    # §15's lesson: a predicate the un-rewritten source also satisfies is not a test. The
    # expected text must differ from the input, or the equality above proves nothing.
    assert REAL_TS_REWRITTEN != REAL_TS_SOURCE
    assert "console.log" not in REAL_TS_REWRITTEN
    # ... and the rewrite must be surgical: every line the rule did not claim survives verbatim.
    before, after = REAL_TS_SOURCE.splitlines(), REAL_TS_REWRITTEN.splitlines()
    assert [i for i, (b, a) in enumerate(zip(before, after, strict=True)) if b != a] == [3]

    # The same driver call reaching real disk: staged text in, rewritten text out, and the
    # post-image still parses under the grammar the probe uses.
    (tmp_path / "app.ts").write_text(apply_in_memory(REAL_TS_SOURCE, patch.diff), encoding="utf-8")
    assert await AstGrepRewriter().parse_probe(str(tmp_path / "app.ts")) is True


async def test_a_mutated_fix_and_an_unmatched_pattern_both_diverge_from_the_expected_bytes() -> (
    None
):
    """Intent: prove the byte assertion above cannot pass without a real rewrite — a different
    `fix` must produce different bytes, and a pattern that matches nothing must produce `None`.

    Reverting `astgrep.py:apply` to a no-op (returning `None`, or staging without invoking the
    CLI) fails `test_astgrep_rewrites_real_typescript_to_exact_bytes` at its first assertion:
    `make_unified_diff` of identical texts is `""`, so `apply` returns `None`.
    """
    driver = AstGrepRewriter()

    mutated = await driver.apply(
        ts_console_rule("mutated", fix="logger.warn($A)"), TS_PATH, REAL_TS_SOURCE, {}
    )
    assert mutated is not None
    mutated_text = apply_in_memory(REAL_TS_SOURCE, mutated.diff)
    assert mutated_text != REAL_TS_REWRITTEN, "the expected bytes do not depend on the `fix`"
    assert mutated_text != REAL_TS_SOURCE
    assert "  logger.warn(x);\n" in mutated_text

    unmatched = real_rule(
        "no-such-call", language="typescript", pattern="neverCalled($A)", fix="logger.info($A)"
    )
    assert await driver.apply(unmatched, TS_PATH, REAL_TS_SOURCE, {}) is None

    # The language gate is the other way this returns `None` without the tool being at fault.
    java_rule = real_rule(
        "wrong-language", language="java", pattern="console.log($A)", fix="logger.info($A)"
    )
    assert await driver.apply(java_rule, TS_PATH, REAL_TS_SOURCE, {}) is None


async def test_astgrep_rewrites_every_match_in_a_multi_match_file() -> None:
    """Intent: one pass must rewrite ALL matches, not just the first — a driver that stopped after
    one would leave a half-migrated file that still compiles and passes a "did it change" check."""
    patch = await AstGrepRewriter().apply(
        ts_console_rule("multi"), "web/trace.ts", MULTI_TS_SOURCE, {}
    )
    assert patch is not None
    rewritten = apply_in_memory(MULTI_TS_SOURCE, patch.diff)

    assert rewritten == MULTI_TS_REWRITTEN
    assert rewritten.count("logger.info(") == 3 == MULTI_TS_SOURCE.count("console.log(")
    assert "console.log" not in rewritten
    assert len(rewritten.encode("utf-8")) == len(MULTI_TS_REWRITTEN.encode("utf-8")) == 147
    assert len(rewritten.splitlines()) == len(MULTI_TS_SOURCE.splitlines()) == 7


async def test_astgrep_rewrites_real_python_bytes_through_the_same_driver() -> None:
    """Intent: `language_for_path` routing is real, not a TypeScript special case — a `.py` file
    goes through ast-grep's python grammar and comes back rewritten to exact bytes."""
    py_rule = real_rule(
        "print-to-logger", language="python", pattern="print($A)", fix="logger.info($A)"
    )
    patch = await AstGrepRewriter().apply(py_rule, PY_PATH, REAL_PY_SOURCE, {})

    assert patch is not None and patch.path == PY_PATH
    assert apply_in_memory(REAL_PY_SOURCE, patch.diff) == REAL_PY_REWRITTEN
    assert REAL_PY_REWRITTEN != REAL_PY_SOURCE
    assert "print(" not in REAL_PY_REWRITTEN
    assert "    y = collect.gather(x)\n" in REAL_PY_REWRITTEN, "untouched lines survive verbatim"
    # The TypeScript rule must NOT fire on the python file, or "routing" would be a coincidence.
    assert await AstGrepRewriter().apply(ts_console_rule(), PY_PATH, REAL_PY_SOURCE, {}) is None


async def test_astgrep_probe_gate_blocks_the_landing_and_no_commit_is_created(
    tmp_path: Path,
) -> None:
    """Intent: a rewrite whose output does not parse must be refused by `apply_patch`'s probe and
    must never reach a commit — the §3.2 step 6.2 gate, asserted at the seam that enforces it."""
    repo = _repo_with(tmp_path, TS_PATH, WRECKED_TS_SOURCE)
    driver = AstGrepRewriter()
    wrecking = ts_console_rule("wreck-the-file", fix="const = = ;")

    patch = await driver.apply(wrecking, TS_PATH, WRECKED_TS_SOURCE, {})
    assert patch is not None
    # Measured, not assumed: the real binary really does emit this, and it really does not parse.
    assert apply_in_memory(WRECKED_TS_SOURCE, patch.diff) == WRECKED_TS_REWRITTEN
    assert await driver.probe_text(TS_PATH, WRECKED_TS_REWRITTEN) is False

    result = await apply_patch(repo, patch, dest_subtree="web", probe=driver.parse_probe)

    assert result == ApplyResult(
        ok=False,
        path=TS_PATH,
        reason="parse probe failed after apply",
        parse_probe_ok=False,
        already_applied=False,
    )
    # The probe judged the bytes `git apply` actually wrote, not a buffer we held.
    assert (repo / TS_PATH).read_text(encoding="utf-8") == WRECKED_TS_REWRITTEN
    # ... and the landing stopped there: `apply_and_commit` was never reached, so HEAD is still
    # the single initial commit. A rejected patch that had already been committed would be a
    # broken file on a branch, which is the whole point of gating BEFORE the commit.
    assert _git_out(repo, "rev-list", "--count", "HEAD") == "1"
    assert _git_out(repo, "log", "--format=%s").splitlines() == ["initial"]


async def test_astgrep_probe_gate_holds_from_any_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Intent: the gate must judge the file `git apply` just wrote, wherever the process happens
    to be standing. `patch.path` is repo-relative, so a probe handed that string bare resolves it
    against the *process* cwd — where it names nothing at all, which `ast-grep` reports with
    exit 0, i.e. "parses". Both halves of that silent pass are pinned here: the path the gate
    hands its probe must be the file inside the worktree, and the landing must be refused.
    """
    repo = _repo_with(tmp_path, TS_PATH, WRECKED_TS_SOURCE)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # deliberately NOT the worktree, and holding no `web/app.ts`

    driver = AstGrepRewriter()
    wrecking = ts_console_rule("wreck-the-file", fix="const = = ;")
    patch = await driver.apply(wrecking, TS_PATH, WRECKED_TS_SOURCE, {})
    assert patch is not None

    probed: list[str] = []

    async def probe(path: str) -> bool:
        probed.append(path)
        return await driver.parse_probe(path)

    result = await apply_patch(repo, patch, dest_subtree="web", probe=probe)

    # The path handed to the probe, not just the verdict: a probe pointed at a file that is not
    # there now answers `False` too, so a verdict-only assertion would pass on a gate that never
    # read the rewritten bytes.
    probed_paths = [Path(p) for p in probed]
    assert probed_paths == [repo / TS_PATH]
    assert probed_paths[0].is_file(), "the probe was pointed at bytes that exist on disk"
    assert result == ApplyResult(
        ok=False,
        path=TS_PATH,
        reason="parse probe failed after apply",
        parse_probe_ok=False,
        already_applied=False,
    )
    assert (repo / TS_PATH).read_text(encoding="utf-8") == WRECKED_TS_REWRITTEN
    assert _git_out(repo, "rev-list", "--count", "HEAD") == "1"


async def test_the_whole_path_pipeline_to_worktree_lands_exact_bytes(tmp_path: Path) -> None:
    """Intent: end to end — real rules through `RewritePipeline` with a real ast-grep engine and a
    real probe, then `git apply` into a real worktree, leaving exactly the expected bytes on disk.
    """
    repo = _repo_with(tmp_path, TS_PATH, REAL_TS_SOURCE)
    rewriter = AstGrepRewriter()
    pipe = RewritePipeline(
        [ts_console_rule()], EngineRegistry([rewriter]), probe=rewriter.probe_text
    )

    outcome = await pipe.rewrite_file(TS_PATH, REAL_TS_SOURCE)
    assert outcome.changed and outcome.applied_rule_ids == ("console-to-logger",)
    assert outcome.text == REAL_TS_REWRITTEN
    assert outcome.patch is not None
    assert outcome.patch.parse_probe_ok is True, "the real probe judged the real rewritten buffer"

    result = await apply_patch(repo, outcome.patch, dest_subtree="web")
    assert result.ok and not result.already_applied, result.reason
    assert (repo / TS_PATH).read_text(encoding="utf-8") == REAL_TS_REWRITTEN
    assert (repo / TS_PATH).read_text(encoding="utf-8") != REAL_TS_SOURCE


# ---------------------------------------------------------------------------------------
# 8. rules loading, matching, templating
# ---------------------------------------------------------------------------------------
def test_load_rules_is_ordered_and_refuses_duplicate_ids(tmp_path: Path) -> None:
    """Ids are the tiebreak in the total order, so a duplicate id makes the order ambiguous —
    a startup error, not a last-one-wins overwrite that silently drops a rule."""
    (tmp_path / "b.yml").write_text(
        "rules:\n"
        "  - {id: zzz, languages: [python], rule: {pattern: x}, priority: 10}\n"
        "  - {id: aaa, languages: [python], rule: {pattern: y}}\n",
        encoding="utf-8",
    )
    loaded = load_rules(tmp_path)
    assert [r.id for r in loaded] == ["zzz", "aaa"]  # priority 10 before 100

    (tmp_path / "a.yml").write_text(
        "- {id: aaa, languages: [python], rule: {pattern: z}}\n", encoding="utf-8"
    )
    with pytest.raises(ConfigValidationError, match="duplicate rule id"):
        load_rules(tmp_path)


def test_load_rules_refuses_a_symlinked_rule_file(tmp_path: Path) -> None:
    """A symlinked rule file must be refused, not followed. `.is_symlink()` is checked before
    opening, and the refusal is loud with a named exception."""
    real_rule = tmp_path / "real.yml"
    real_rule.write_text(
        "rules:\n  - {id: real, languages: [python], rule: {pattern: x}}\n",
        encoding="utf-8",
    )
    symlink_target = tmp_path / "outside.yml"
    symlink_target.write_text(
        "rules:\n  - {id: symlinked, languages: [python], rule: {pattern: y}}\n",
        encoding="utf-8",
    )
    symlink = tmp_path / "link.yml"
    symlink.symlink_to(symlink_target)

    assert symlink.is_symlink(), "fixture precondition: a REAL OS-level symlink"
    with pytest.raises(ConfigFileError, match="refusing to load a symlink"):
        load_rules(tmp_path)


def test_glob_matching_never_crosses_a_directory_separator() -> None:
    """`applies_to: ['*.py']` means top-level, and `**/` spans zero or more directories."""
    top = RewriteRule(id="t", languages=["python"], applies_to=["*.py"], rule={"pattern": "x"})
    deep = RewriteRule(id="d", languages=["python"], applies_to=["**/*.py"], rule={"pattern": "x"})
    assert rule_matches_path(top, "setup.py")
    assert not rule_matches_path(top, "pkg/mod.py")
    assert rule_matches_path(deep, "setup.py") and rule_matches_path(deep, "a/b/mod.py")
    assert not rule_matches_path(deep, "a/b/mod.java"), "language gate, not just the glob"


def test_render_template_refuses_an_unsupplied_placeholder() -> None:
    """A surviving `{{new_pkg}}` becomes a compile error one phase later with no rule id on it."""
    assert render_template("import {{new_pkg}}.X", {"new_pkg": "com.acme"}) == "import com.acme.X"
    with pytest.raises(KeyError, match="new_pkg"):
        render_template("import {{new_pkg}}.X", {})


# ---------------------------------------------------------------------------------------
# 9. diff primitives
# ---------------------------------------------------------------------------------------
def test_diff_round_trip_preserves_a_missing_trailing_newline() -> None:
    """Dropping `\\ No newline at end of file` appends a byte no rule authored — a content change
    that shows up in the PR as noise and in a checksum as a difference."""
    before, after = "a\nb", "a\nB"
    diff = make_unified_diff("x.py", before, after)
    assert "\\ No newline at end of file" in diff
    assert apply_in_memory(before, diff) == after


def test_apply_in_memory_refuses_a_hunk_whose_context_does_not_match() -> None:
    """Exact context, no fuzz: fuzzy application is how a patch lands in the wrong place and
    still exits zero."""
    diff = make_unified_diff("x.py", "a\nb\nc\n", "a\nB\nc\n")
    with pytest.raises(Exception, match="does not match"):
        apply_in_memory("q\nw\ne\n", diff)


async def test_the_composed_patch_survives_a_real_git_apply(tmp_path: Path) -> None:
    """The end of the headline claim, against real git rather than our own diff parser.

    "One patch per file" is only worth anything if `git apply` accepts it, and the composed patch
    is exactly the artefact that used to be two diffs against the same original, the second of
    which git refused. A second apply must report `already_applied` rather than re-applying —
    the §11.7 contract for a partial re-run.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg").mkdir()
    (repo / PATH).write_text(SOURCE, encoding="utf-8")
    _git_init(repo)

    engine = FakeRewriter(
        {
            "r1": lambda t: t.replace("beta", "BETA"),
            "r2": lambda t: t.replace("gamma", "GAMMA"),
        }
    )
    outcome = await pipeline([rule("r1"), rule("r2")], engine).rewrite_file(PATH, SOURCE)
    assert outcome.patch is not None

    result = await apply_patch(repo, outcome.patch, dest_subtree="pkg")
    assert result.ok and not result.already_applied, result.reason
    assert (repo / PATH).read_text(encoding="utf-8") == "alpha\nBETA\nGAMMA\ndelta\n"

    again = await apply_patch(repo, outcome.patch, dest_subtree="pkg")
    assert again.ok and again.already_applied


def test_check_diff_rejects_paths_escaping_the_destination_subtree() -> None:
    """§3.2 step 6.6: an out-of-tree write is rejected BEFORE the mutation is journalled, so it
    is never even intended."""
    escaping = make_unified_diff("../../etc/passwd", "a\n", "b\n")
    assert check_diff(escaping, "libs/acme") is not None
    assert asyncio.run(validate_diff(escaping, "libs/acme")) is False
    inside = make_unified_diff("libs/acme/mod.py", "a\n", "b\n")
    assert check_diff(inside, "libs/acme") is None
    assert check_diff(inside, "libs/acme", max_bytes=10) is not None  # §11.3 patch ceiling


def test_check_diff_rejects_a_declared_path_that_disagrees_with_its_own_diff() -> None:
    """`FilePatch.path` and `FilePatch.diff` can come from two independently model-supplied
    fields (`ProposedFileEdit.path` / `.diff`, `llm/schemas.py`) with nothing forcing them to
    agree. `git apply` only ever looks at the diff's own `---`/`+++` headers, so a mismatch means
    the file actually written and the file `patch.path` claims was written are different files —
    `apply_patch`'s post-apply probe and `cli._transform_criterion`'s parse probe (fed by
    `output.rewritten`, which is `patch.path`) would both check the wrong one.
    """
    diff = make_unified_diff("pkg/real.py", "a\n", "b\n")
    reason = check_diff(diff, "pkg", declared_path="pkg/decoy.py")
    assert reason is not None
    assert "pkg/decoy.py" in reason and "pkg/real.py" in reason


def test_check_diff_accepts_a_declared_path_that_matches_its_own_diff() -> None:
    """The regression pin for the case above: a self-consistent patch — the ONLY shape every
    deterministic call site (`pipeline._finish`, `AstGrepRewriter.apply`) actually produces,
    since both build `patch.path` and the diff from the same local `path` variable — must not be
    caught by the new check."""
    diff = make_unified_diff("pkg/real.py", "a\n", "b\n")
    assert check_diff(diff, "pkg", declared_path="pkg/real.py") is None


def test_check_diff_accepts_a_rename_diffs_destination_as_the_declared_path() -> None:
    """The case most likely to make a naive `path == the only diff path` check wrong: a rename
    (or a rename-plus-edit) diff has a pre-image `---` path and a different post-image `+++`
    path. `diff_paths` reports only the post-image path — `FileDiff.path`'s own docstring says
    it is "what gets written" — so a legitimate rename whose declared path is the DESTINATION
    must be accepted, and one declared as the stale SOURCE path must not be."""
    diff = "--- a/pkg/old.py\n+++ b/pkg/new.py\n@@ -1 +1 @@\n-a\n+b\n"
    assert diff_paths(diff) == ("pkg/new.py",)
    assert check_diff(diff, "pkg", declared_path="pkg/new.py") is None
    reason = check_diff(diff, "pkg", declared_path="pkg/old.py")
    assert reason is not None and "pkg/old.py" in reason


def test_check_diff_accepts_any_path_a_multi_file_diff_actually_writes() -> None:
    """`declared_path in diff_paths(diff)` (membership), not equality: the schema allows one
    `FilePatch` to carry a diff touching several files (`LlmPatchProposal.files` composes several
    `FilePatch`es, but nothing stops a single `ProposedFileEdit.diff` from itself being a
    multi-file unified diff). If that shape is ever legitimately produced, the declared path only
    needs to be ONE of the files the diff writes, not the only one — equality would wrongly
    reject it."""
    diff = (
        "--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -1 +1 @@\n-1\n+2\n"
        "--- a/pkg/b.py\n+++ b/pkg/b.py\n@@ -1 +1 @@\n-3\n+4\n"
    )
    assert diff_paths(diff) == ("pkg/a.py", "pkg/b.py")
    assert check_diff(diff, "pkg", declared_path="pkg/a.py") is None
    assert check_diff(diff, "pkg", declared_path="pkg/b.py") is None
    assert check_diff(diff, "pkg", declared_path="pkg/c.py") is not None


async def test_apply_patch_rejects_a_filepatch_whose_declared_path_disagrees_with_its_diff(
    tmp_path: Path,
) -> None:
    """End-to-end regression for the gap `_as_patches` opens (`workers/rewrite.py`): the LLM
    repair branch lifts `FilePatch(path=edit.path, diff=edit.diff, ...)` from a model response
    where `path` and `diff` are two separate fields the model fills in independently. A patch
    that declares one path while its diff writes another must be rejected before `git apply` ever
    runs — and the file the diff WOULD have written must be left untouched, proving this fires
    before the write rather than after."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "real.py").write_text("a\n", encoding="utf-8")
    _git_init(repo)

    diff = make_unified_diff("pkg/real.py", "a\n", "b\n")
    mismatched = FilePatch(
        path="pkg/decoy.py", diff=diff, tier=TransformTier.DETERMINISTIC, parse_probe_ok=False
    )
    result = await apply_patch(repo, mismatched, dest_subtree="pkg")
    assert not result.ok
    assert result.reason is not None
    assert "pkg/decoy.py" in result.reason and "pkg/real.py" in result.reason
    assert (repo / "pkg" / "real.py").read_text(encoding="utf-8") == "a\n"

    matching = FilePatch(
        path="pkg/real.py", diff=diff, tier=TransformTier.DETERMINISTIC, parse_probe_ok=False
    )
    landed = await apply_patch(repo, matching, dest_subtree="pkg")
    assert landed.ok, landed.reason
    assert (repo / "pkg" / "real.py").read_text(encoding="utf-8") == "b\n"
