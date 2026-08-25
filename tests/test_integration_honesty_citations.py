"""Resolve the `file:line` citations in ``docs/INTEGRATION_HONESTY.md`` against the tree.

Why this module exists
----------------------
The ledger is dense with ``file:line`` citations and, until this file, **nothing in the
suite constructed a path to it** — so no citation in it was ever checked by anything.
Round G paid for that five times: a correction commit wrote 9 of 9 wrong ``src/``
citations (uniformly ``+3``, measured at one commit and landed after another); the review
that caught it had its own boundary falsified by a commit moving ``cli.py`` by ``-19``; a
re-sweep of 63 citations followed; one citation could not be repointed at all because its
target was deleted; and an earlier lane found 13 drifted citations where a brief named 4.

The form, not the values, is what fails
---------------------------------------
A bare ``file:line`` cannot survive an unrelated edit above it. The ledger already carries
a drift-resistant alternative -- a line number **bound to the commit it was measured at**,
written ``(`:7181` at `53e5d8d`)``. That is a claim about a *past* tree: it is correct
forever, it is **exempt** here, and re-pointing it would destroy the record. See
``test_a_commit_bound_citation_is_never_offered_for_resolution``.

What this instrument watches, and why drift cannot leave it unchanged
---------------------------------------------------------------------
For each citation that carries an **anchor** -- a backticked identifier immediately
adjacent to it, as in ``` `LlmConcurrency.for_tier` (`settings.py:227-230`) ``` -- the
watched quantity is:

    whether the cited line range is **contained in** the logical span of that
    identifier's definition in the cited file (see ``_logical_span``).

Drift *is* motion of that span while the cited range stands still, so a rotted citation
takes a contained range to an uncontained one. It cannot leave the quantity unchanged.
Two weaker rules were measured and rejected, and both would have been vacuous greens:

  * "the name occurs as a token inside the cited range" certified
    ``ContainerSandbox.remove()`` (`container.py:202-208`) GREEN against a definition at
    345-358, because the common token ``remove`` appears in that range by coincidence.
    3 of 22 greens were green only that way.
  * "the cited range *intersects* the span" tolerates drift up to the whole extent of a
    definition, so it certified two citations GREEN that had **already drifted inside
    round G**: `WaveScheduler.elapsed_s` (`scheduler.py:423-428`), repointed at
    ``f5a188a`` and since moved to 425-430, and `WaveReport.exit_code`
    (`src/fleet/orchestrator/runner.py:311-320`), since moved to 315-324. Containment
    fails both.

Sensitivity, measured rather than asserted. Of the 14 citations green at ``3dd500d``, the
smallest drift that would redden each is 1 line for six of them, 2 for five, 3 for two,
and 25 for `phase_floor` (`src/fleet/orchestrator/reentry.py:96-103`) -- the one citation
naming the middle of a long definition rather than the definition itself. That last number
is the honest statement of this instrument's blind spot: a citation into the interior of a
definition tolerates drift up to that definition's extent.

Two nearby quantities are useless here and are not what this module keys on: "the file exists" and
"the file still has that many lines" are both **invariant under drift** (all 409 resolvable
citations are in range at ``3dd500d``, and 38 of them are nonetheless unresolved), and a
whole-file digest would move under any edit at all.

Scope, stated so it is not mistaken for more
--------------------------------------------
Only ``docs/INTEGRATION_HONESTY.md``. Only citations that name a path; the ledger also
holds 343 bare ``:N`` continuation citations whose file comes from surrounding prose, and
none of those are covered. The anchored check runs on ``.py`` targets only (AST); ``.md``
and ``.sql`` citations get the existence/in-range checks and nothing more.

The pinned set is not an exemption list
---------------------------------------
``_PINNED_UNRESOLVED`` records every anchored citation that does **not** resolve at the
commit this module landed at. It is a ratchet, not a whitelist: it fails in **both**
directions, by ``file:line``. A new unresolved citation fails
``test_no_unpinned_anchored_citation_fails_to_resolve``; a pinned one that starts resolving
(or whose text changes at all) fails its own
``test_each_pinned_citation_is_still_unresolved`` case. It therefore cannot rot silently,
which a hand-maintained exemption list can. Its members have **three different causes**
and this module does not claim to separate them -- the ledger marks records in prose, not
in syntax, so no rule here can:
  * genuine drift -- verified independently against history for the ``settings.py``
    cluster: at ``a1178f7`` ``on_exhausted`` was at line 428 and ``clone_timeout_s`` at
    268, exactly as cited, and both moved ``+8`` by ``a9afe9d``;
  * a citation that deliberately names a **usage** site, not the definition (the ledger
    says so of ``response.usage``);
  * a citation inside a passage kept **verbatim as a record**, which must not be repointed.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LEDGER_REL = "docs/INTEGRATION_HONESTY.md"

# The index is built from these directories ONLY -- never the repo root, recursively or
# otherwise. That is what keeps the verdict identical in a detached worktree and in the
# primary checkout: anything rooted at the repo top sees `.venv`/`bazel-*`/`migration_state.json`
# in one tree and not the other (D85's class -- a sibling instrument read 182 files in a
# worktree and 6,975 in the primary). An earlier draft here also scanned repo-root files;
# it indexed 259 in each tree only by coincidence (the primary carries an untracked
# `migration_state.json`, the worktree carries this module), and **zero** of the 414
# citations resolve to a repo-root file, so the scan bought nothing and was dropped.
# Measured at 3dd500d: 256 files indexed in the worktree holding this module and 255 in
# the primary, which does not yet hold it -- the whole difference is this file. The verdict --
# 414 citations / 409 uniquely resolved / 5 ambiguous / 2 commit-bound / 60 anchored /
# 46 unresolved / 0 unpinned failures / 0 stale pins -- is identical in both.
# A citation naming a repo-root file now fails loudly in
# `test_every_pathed_citation_names_a_file_that_exists`, naming the directories searched.
_SEARCH_DIRS = ("src", "tests", "docs")
_EXTS = frozenset(
    {".py", ".md", ".sql", ".toml", ".yaml", ".yml", ".json", ".bzl", ".txt", ".cfg", ".ini", ".sh"}
)

_EXT_ALT = "|".join(sorted(e.lstrip(".") for e in _EXTS))
_CITATION = re.compile(
    r"`(?P<path>[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:" + _EXT_ALT + r"))(?P<lines>(?::\d+(?:-\d+)?)+)`"
)
# The drift-resistant form already in the ledger: a line bound to its commit. Exempt.
_COMMIT_BOUND = re.compile(r"\(`:\d+(?:-\d+)?`\s+at\s+`[0-9a-f]{7,40}`\)")
# An anchor immediately adjacent to a parenthesised citation: `X` (`path:lines`).
_ANCHORED = re.compile(
    r"`(?P<anchor>[^`\n]{3,120})`[,;]?\s*\((?:[^()`]{0,40}\s)?"
    r"`(?P<path>[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:" + _EXT_ALT + r"))(?P<lines>(?::\d+(?:-\d+)?)+)`"
)
_IDENT_PATH = re.compile(r"^\.?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?:\(\))?$")


# Every anchored citation that did not resolve at 3dd500d, as (anchor, citation).
# Both-directions ratchet -- see the module docstring. Do not add to this without a
# measurement; do not delete from it without repointing the citation.
_PINNED_UNRESOLVED: tuple[tuple[str, str], ...] = (
    (
        "test_every_file_the_generated_files_name_exists_after_the_phase_that_writes_them",
        "tests/test_workers_build.py:1695",
    ),  # L169 defined at [(3301, 3398), (3301, 3398)], cited 1695-1695
    (
        "GoModuleCoordinateError",
        "go.py:92",
    ),  # L787 defined at [(120, 146), (120, 146)], cited 92-92
    ("generate_targets()", "go.py:311"),  # L787 defined at [(364, 368)], cited 311-311
    ("resolution()", "go.py:191"),  # L787 defined at [(297, 363)], cited 191-191
    ("resolution()", "go.py:244"),  # L787 defined at [(297, 363)], cited 244-244
    ("workspace_files", "go.py:188"),  # L787 defined at [(241, 296)], cited 188-188
    (
        "buildverify._bazel_argv",
        "buildverify.py:853",
    ),  # L1441 defined at [(1112, 1147)], cited 853-853
    (
        "test_a_docker_run_that_exits_125_is_not_reported_as_a_broken_build_file",
        "tests/test_workers_build.py:1333",
    ),  # L1970 defined at [(1365, 1448), (1365, 1448)], cited 1333-1333
    ("Git.resolve", "vcs/git.py:265-271"),  # L2022 defined at [(309, 326)], cited 265-271
    (
        ".diagnosis_failure_class",
        "buildverify.py:625-630",
    ),  # L2442 defined at [(636, 638)], cited 625-630
    (
        "BuildWorker._handoff",
        "cli.py:4983-4989",
    ),  # L2459 defined at [(3507, 3547), (5577, 5611), (5811, 5837)], cited 4983-4989
    ("response.usage", "buildverify.py:1158"),  # L2468 defined at [(908, 908)], cited 1158-1158
    (
        "_phase_preflight",
        "cli.py:866-878",
    ),  # L2529 defined at [(936, 950), (936, 950)], cited 866-878
    (
        "apply_and_commit",
        "vcs/commits.py:232-264",
    ),  # L2640 defined at [(261, 295), (261, 295)], cited 232-264
    ("_record", "workers/rewrite.py:535-541"),  # L2645 defined at [(567, 587)], cited 535-541
    (
        "TransformInput",
        "cli.py:3200-3218",
    ),  # L2685 defined at [(3312, 3339), (3312, 3339)], cited 3200-3218
    (
        "max_patch_bytes",
        "cli.py:3219-3224",
    ),  # L2727 defined at [(3332, 3338), (4165, 4165)], cited 3219-3224
    (
        "TransformInput",
        "cli.py:4025",
    ),  # L2728 defined at [(3312, 3339), (3312, 3339)], cited 4025-4025
    (
        "RunContext.llm_policy",
        "orchestrator/context.py:140",
    ),  # L2764 defined at [(188, 188)], cited 140-140
    ("FleetSettings.load", "settings.py:1105"),  # L2870 defined at [(1109, 1187)], cited 1105-1105
    (
        "concurrency.llm.workhorse",
        "settings.py:224",
    ),  # L2934 defined at [(232, 232)], cited 224-224
    (
        "LlmConcurrency.for_tier",
        "settings.py:227-230",
    ),  # L2935 defined at [(234, 238)], cited 227-230
    (".on_exhausted", "settings.py:427-428"),  # L2958 defined at [(436, 436)], cited 427-428
    (".clone_timeout_s", "settings.py:267-268"),  # L2976 defined at [(276, 276)], cited 267-268
    ("llm.max_schema_repairs", "settings.py:680"),  # L3019 defined at [(688, 688)], cited 680-680
    (
        "_transform_payloads",
        "cli.py:4028",
    ),  # L3072 defined at [(4155, 4201), (4155, 4201)], cited 4028-4028
    (
        "cli._TransformEvidence.record()",
        "cli.py:3469-3480",
    ),  # L3169 defined at [(3595, 3611)], cited 3469-3480
    (
        "ContainerSandbox.remove()",
        "container.py:202-208",
    ),  # L3233 defined at [(344, 358)], cited 202-208
    ("reap()", "container.py:239-254"),  # L3234 defined at [(456, 532)], cited 239-254
    (
        "_reap_lines",
        "cli.py:10319",
    ),  # L4731 defined at [(10869, 10919), (10869, 10919)], cited 10319-10319
    (
        "OrchestratorContext.worktree",
        "orchestrator/context.py:206-208",
    ),  # L4771 defined at [(265, 268)], cited 206-208
    (
        "WorktreeManager.reap",
        "sandbox/worktree.py:303-310",
    ),  # L4773 defined at [(343, 379)], cited 303-310
    ("_git_run", "sandbox/worktree.py:134-141"),  # L4780 defined at [(172, 180)], cited 134-141
    (
        "cli._reap_worktree_manager",
        "cli.py:10464-10471",
    ),  # L4781 defined at [(11984, 11992), (11984, 11992)], cited 10464-10471
    (
        "_no_test_may_reach_the_host_docker_daemon",
        "tests/test_cli.py:1609-1617",
    ),  # L4810 defined at [(1914, 1924), (1914, 1924)], cited 1609-1617
    (
        "SqliteSchedulerStore.append_blocked_by",
        "src/fleet/orchestrator/scheduler.py:255",
    ),  # L4994 defined at [(273, 306)], cited 255-255
    (
        "RepoState._stub_invariants",
        "models/state.py:188-193",
    ),  # L5036 defined at [(206, 220)], cited 188-193
    (
        "WaveScheduler.elapsed_s",
        "scheduler.py:423-428",
    ),  # L5856 defined at [(424, 430)], cited 423-428
    (
        "WaveScheduler.breached",
        "scheduler.py:430-437",
    ),  # L5857 defined at [(431, 439)], cited 430-437
    (
        "WaveReport.exit_code",
        "src/fleet/orchestrator/runner.py:311-320",
    ),  # L5865 defined at [(314, 324)], cited 311-320
    (
        "_run_transform_wave",
        "cli.py:4445",
    ),  # L6239 defined at [(4202, 4272), (4202, 4272)], cited 4445-4445
    (
        "_prepare_repo",
        "cli.py:3858",
    ),  # L6242 defined at [(3873, 3978), (3873, 3978)], cited 3858-3858
    (
        "_run_verify_wave",
        "cli.py:8451",
    ),  # L6256 defined at [(7748, 7829), (7748, 7829)], cited 8451-8451
    (
        "_wave_snapshot",
        "cli.py:8237-8239",
    ),  # L6330 defined at [(7358, 7380), (7358, 7380)], cited 8237-8239
    (
        "WaveScheduler.breached",
        "scheduler.py:430-437",
    ),  # L6344 defined at [(431, 439)], cited 430-437
    (
        "_ruff()",
        "tests/test_lint_gate.py:63",
    ),  # L6378 defined at [(69, 110), (69, 110)], cited 63-63
)


@dataclass(frozen=True)
class Citation:
    """One ``path:lines`` citation as it stands in the ledger."""

    doc_line: int
    cited_path: str
    lines_text: str
    resolved: str | None
    ambiguous: tuple[str, ...] = ()

    @property
    def span(self) -> tuple[int, int]:
        nums = [int(n) for n in re.findall(r"\d+", self.lines_text)]
        return min(nums), max(nums)

    @property
    def text(self) -> str:
        return f"{self.cited_path}{self.lines_text}"

    @property
    def site(self) -> str:
        return f"{_LEDGER_REL}:{self.doc_line}"


@dataclass(frozen=True)
class Anchored:
    """A citation carrying an adjacent identifier anchor, plus its resolution verdict."""

    citation: Citation
    anchor: str
    resolves: bool
    detail: str
    error: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.anchor, self.citation.text)


@dataclass
class Survey:
    root: Path
    indexed_files: int
    citations: list[Citation] = field(default_factory=list)
    commit_bound: list[int] = field(default_factory=list)
    anchored: list[Anchored] = field(default_factory=list)

    def anchored_by_key(self, key: tuple[str, str]) -> Anchored | None:
        for a in self.anchored:
            if a.key == key:
                return a
        return None


def _build_index(root: Path) -> tuple[dict[str, set[str]], int]:
    index: dict[str, set[str]] = {}
    count = 0

    def add(rel: str) -> None:
        parts = rel.split("/")
        for i in range(len(parts)):
            index.setdefault("/".join(parts[i:]), set()).add(rel)

    for name in _SEARCH_DIRS:
        base = root / name
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*")):
            if f.is_file() and f.suffix in _EXTS:
                count += 1
                add(f.relative_to(root).as_posix())
    return index, count


def _normalised(text: str) -> tuple[str, list[int]]:
    """Collapse every whitespace run to one space, keeping an offset map back to ``text``.

    A line-oriented sweep certifies a class as fixed when it is not: a citation whose
    anchor sits on the previous physical line is invisible to it. This module therefore
    matches on the normalised string and maps every hit back to a real line number.
    """
    out: list[str] = []
    omap: list[int] = []
    prev_ws = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if not prev_ws:
                out.append(" ")
                omap.append(i)
            prev_ws = True
        else:
            out.append(ch)
            omap.append(i)
            prev_ws = False
    return "".join(out), omap


def _logical_span(node: ast.AST, body: list[str]) -> tuple[int, int]:
    """The lines a careful author would cite for ``node``, widened from the raw AST span.

    Three widenings, each derived from the source rather than from a tolerance constant --
    a bare AST span produces false "drifted" verdicts on citations that are exactly right:
      * **decorators** -- ``lineno`` of a decorated ``def`` is the ``def`` line, so a
        citation that includes ``@property`` starts above the span;
      * **leading blank/comment lines** -- ``docker_run_argv`` (`container.py:127-133`)
        cites the blank line before its ``def`` at 128;
      * **trailing comment lines** -- ``end_lineno`` stops at the closing bracket, so
        ``TERMINAL_STATUSES`` (`models/enums.py:25-28`) cites one line past a span ending
        at 27, and the three lines after it are that statement's own explanatory comment.
    There is no numeric slack anywhere: each edge stops at the first line that is neither
    blank nor a comment.
    """
    start = node.lineno  # type: ignore[attr-defined]
    for dec in getattr(node, "decorator_list", ()):
        start = min(start, dec.lineno)
    end = getattr(node, "end_lineno", None) or start
    while start > 1:
        prev = body[start - 2].strip()
        if prev and not prev.startswith("#"):
            break
        start -= 1
    while end < len(body):
        nxt = body[end].strip()
        if not nxt.startswith("#"):
            break
        end += 1
    return start, end


def _symbol_table(path: Path) -> dict[str, list[tuple[int, int]]]:
    body = path.read_text(encoding="utf-8").split("\n")
    table: dict[str, list[tuple[int, int]]] = {}

    def walk(node: ast.AST, prefix: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = [*prefix, child.name]
                span = _logical_span(child, body)
                table.setdefault(".".join(qual), []).append(span)
                table.setdefault(child.name, []).append(span)
                walk(child, qual)
            elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                span = _logical_span(child, body)
                for tgt in targets:
                    if isinstance(tgt, ast.Name):
                        table.setdefault(".".join([*prefix, tgt.id]), []).append(span)
                        table.setdefault(tgt.id, []).append(span)

    walk(ast.parse("\n".join(body)), [])
    return table


def build_survey(root: Path) -> Survey:
    """Read the ledger and resolve every citation in it. All I/O for this module is here.

    Per-citation failures are **captured**, never raised: a single unresolvable citation
    must cost one check, not the whole module. That is the property measured in
    ``test_a_single_unresolvable_citation_costs_one_check_not_the_module``.
    """
    ledger = root / _LEDGER_REL
    text = ledger.read_text(encoding="utf-8")
    norm, omap = _normalised(text)
    index, indexed = _build_index(root)
    survey = Survey(root=root, indexed_files=indexed)

    def doc_line(npos: int) -> int:
        return text.count("\n", 0, omap[npos]) + 1

    for m in _COMMIT_BOUND.finditer(norm):
        survey.commit_bound.append(doc_line(m.start()))

    def cite_of(m: re.Match[str]) -> Citation:
        cited = m.group("path")
        hits = sorted(index.get(cited, ()))
        return Citation(
            doc_line=doc_line(m.start()),
            cited_path=cited,
            lines_text=m.group("lines"),
            resolved=hits[0] if len(hits) == 1 else None,
            ambiguous=tuple(hits) if len(hits) > 1 else (),
        )

    for m in _CITATION.finditer(norm):
        survey.citations.append(cite_of(m))

    tables: dict[str, dict[str, list[tuple[int, int]]]] = {}
    for m in _ANCHORED.finditer(norm):
        anchor = m.group("anchor").strip()
        cite = cite_of(m)
        if cite.resolved is None or not cite.resolved.endswith(".py"):
            continue
        if not _IDENT_PATH.match(anchor):
            continue  # not an identifier -- prose or a quoted expression, not our class
        key = anchor.rstrip("()").lstrip(".")
        try:
            if cite.resolved not in tables:
                tables[cite.resolved] = _symbol_table(root / cite.resolved)
            table = tables[cite.resolved]
        except (OSError, SyntaxError, ValueError) as exc:
            survey.anchored.append(
                Anchored(cite, anchor, False, "", error=f"{type(exc).__name__}: {exc}")
            )
            continue
        parts = key.split(".")
        spans: list[tuple[int, int]] | None = None
        for i in range(len(parts)):
            spans = table.get(".".join(parts[i:]))
            if spans:
                break
        if not spans:
            continue  # the anchor names nothing this file defines -- not our class
        lo, hi = cite.span
        contained = any(s <= lo and hi <= e for s, e in spans)
        survey.anchored.append(
            Anchored(cite, anchor, contained, f"defined at {spans[:3]}, cited {lo}-{hi}")
        )
    return survey


@pytest.fixture(scope="session")
def ledger_survey() -> Survey:
    """Resolution happens HERE, not at import and not in a ``parametrize`` argument.

    A locator evaluated inside a ``parametrize`` decorator turns any single-site failure
    into a module-wide **collection** error -- this project measured one non-Python line
    taking 20 passing checks to 0 executed. Everything below parametrizes over module
    literals only.
    """
    return build_survey(_REPO_ROOT)


def test_every_pathed_citation_names_a_file_that_exists(ledger_survey: Survey) -> None:
    bad = [
        f"{c.site}: `{c.text}` -> no such file under {_SEARCH_DIRS}"
        for c in ledger_survey.citations
        if c.resolved is None and not c.ambiguous
    ]
    assert not bad, "citations naming a file that does not exist:\n" + "\n".join(bad)


def test_every_pathed_citation_points_at_a_line_the_file_still_has(ledger_survey: Survey) -> None:
    """Catches the deletion/truncation case -- one round-G citation could not be repointed
    at all because its target had been deleted. Invariant under drift, deliberately: it is
    a different quantity from the anchored check, not a weaker version of it."""
    bad = []
    for c in ledger_survey.citations:
        if c.resolved is None:
            continue
        length = len((ledger_survey.root / c.resolved).read_text(encoding="utf-8").split("\n"))
        lo, hi = c.span
        if lo < 1 or hi > length:
            bad.append(f"{c.site}: `{c.text}` -> {c.resolved} has {length} lines")
    assert not bad, "citations pointing past the end of their file:\n" + "\n".join(bad)


def test_no_unpinned_anchored_citation_fails_to_resolve(ledger_survey: Survey) -> None:
    """The drift detector. Fails by ``file:line``, naming the citation and where its
    anchor actually is -- never by count, which tells the next author nothing."""
    pinned = set(_PINNED_UNRESOLVED)
    bad = [
        f"{a.citation.site}: `{a.anchor}` (`{a.citation.text}`) -> {a.error or a.detail}"
        for a in ledger_survey.anchored
        if not a.resolves and a.key not in pinned
    ]
    assert not bad, (
        "anchored citations that no longer resolve (repoint them, or pin them with a "
        "measurement if the citation names a usage site or sits inside a verbatim "
        "record):\n" + "\n".join(bad)
    )


@pytest.mark.parametrize("pin", _PINNED_UNRESOLVED, ids=lambda p: f"{p[0]}|{p[1]}")
def test_each_pinned_citation_is_still_unresolved(
    pin: tuple[str, str], ledger_survey: Survey
) -> None:
    """The other direction of the ratchet, one case per pin.

    Parametrised over a module literal -- no locator runs in the decorator -- so a site
    that blows up costs exactly this one case and the module still imports.
    """
    found = ledger_survey.anchored_by_key(pin)
    assert found is not None, (
        f"pinned citation `{pin[0]}` (`{pin[1]}`) is no longer in the ledger in that form. "
        "If it was repointed, delete this entry from _PINNED_UNRESOLVED."
    )
    if found.error is not None:
        raise AssertionError(f"{found.citation.site}: resolving `{pin[1]}` failed: {found.error}")
    assert not found.resolves, (
        f"{found.citation.site}: `{pin[0]}` (`{pin[1]}`) now resolves ({found.detail}). "
        "Delete this entry from _PINNED_UNRESOLVED."
    )


def test_a_commit_bound_citation_is_never_offered_for_resolution(ledger_survey: Survey) -> None:
    """``(`:7181` at `53e5d8d`)`` is a claim about a past tree: correct forever, and
    repointing it would destroy the record. It carries no path, so ``_CITATION`` cannot
    match it and no check above can ever demand it resolve. This asserts that exemption
    rather than trusting it, and asserts the form is still present to be exempted."""
    assert ledger_survey.commit_bound, (
        "no commit-bound citation found in the ledger -- the drift-resistant form this "
        "module exempts has disappeared, so the exemption is now untested"
    )
    ledger_text = (ledger_survey.root / _LEDGER_REL).read_text(encoding="utf-8")
    for m in _COMMIT_BOUND.finditer(ledger_text):
        assert not _CITATION.search(m.group(0)), (
            f"a commit-bound citation {m.group(0)!r} was matched as a live citation"
        )
