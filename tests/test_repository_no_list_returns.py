"""`docs/SPEC.md` §12 item 22, one of its four bundled sub-clauses: "...no `state/repository.py`
function returns a `list` for `symbols`, `edges`, `events`, or `attempts` (asserted by
return-type inspection)."

WHY this file exists
---------------------
research-17 (round VI) found **zero** matches for `grep -n "-> list\\[" src/fleet/state/
repository.py` and flagged that as a narrow heuristic: it only catches the exact literal
`-> list[` and would miss a bare `list` with no generic parameter, a `list[...] | None` union,
or a `typing.List` spelling. What was missing either way was the DEDICATED test the criterion's
own wording asks for — this file is that test, built after re-deriving the check by hand (see
below) rather than trusting the narrow grep.

The quantity this instrument watches
-------------------------------------
Every `def`/`async def` in `src/fleet/state/repository.py` (module-level, Protocol stub, or
class method — `ast`-walked, so a nested closure would be seen too even though none currently
matches) whose name, split on `_`, contains one of the whole tokens `symbol(s)`, `edge(s)`,
`event(s)`, `attempt(s)`. **Token-based, not substring-based.** A first attempt at this selector
used a plain substring search for "edge" and it matched every `*_ledger` function in this module
(`open_budget_ledger`, `halt_budget_ledger`, `_probe_repo_ledger`, ...) — "ledger" contains
"edge" as a substring (l-**edge**-r). `test_detector_is_silent_on_an_unrelated_ledger_function_
despite_containing_edge` below pins that this class of false positive stays fixed.

As measured at HEAD (`e4dae942`), the matched set is exactly eight distinct names, each once in
the `StateRepository`/`ReadOnlyRepository` Protocol and once in `SqliteStateRepository`:
`iter_symbols`, `iter_edges`, `iter_events`, `iter_attempts`, `insert_symbols`, `insert_edges`,
`append_event`, `record_attempt`. The four `iter_*` methods are exactly §11's streaming
surface (declared `-> AsyncIterator[...]`, implemented as `async def` generators using
`fetchmany()` + `yield`, confirmed by reading the whole file — never `fetchall()` or
`list(...)` for these four tables). The other four take a row (or rows) as INPUT and return an
`int` count or `None`; they are included anyway because the selector matches on name, not on
"is this the read side" — a future `list_symbols`-shaped function landing anywhere in this file
would be caught by the same net rather than requiring the four names to be re-guessed.

Two kinds of check, per CLAUDE.md Guardrail 6
-----------------------------------------------
1. **The real assertion** (`test_no_symbols_edges_events_attempts_function_returns_a_list`):
   walk the real file, assert the matched set is non-empty (a detector examining zero functions
   "passes" vacuously and proves nothing), then assert none of them has a `list`-shaped return
   annotation.
2. **Instrument validation** — a detector never observed firing is not evidence it works:
   - fires on a synthetic `-> list[int]` decoy (the exact shape research-17's grep DID catch),
   - fires on a bare `-> list` decoy (the shape a `"-> list["` string search misses),
   - fires on a `-> list[int] | None` decoy (a second shape a literal string search misses),
   - stays silent on the `*_ledger` false-positive class (the token-boundary regression),
   - stays silent on a cosmetic reflow of the real file (`ast.unparse` of the real parse tree:
     same structure, different literal text/whitespace — proves the detector reads structure,
     not layout).

This module also does not touch `src/fleet/state/repository.py` itself: research-17's grep and
this file's own from-scratch walk (independently derived) agree that no violation exists today.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPOSITORY_PY = (
    Path(__file__).resolve().parent.parent / "src" / "fleet" / "state" / "repository.py"
)

#: Whole underscore-delimited tokens this test treats as "plausibly returns symbols/edges/
#: events/attempts data". See the module docstring for why this is token-based rather than a
#: substring search.
_STEMS = frozenset(
    {
        "symbol",
        "symbols",
        "edge",
        "edges",
        "event",
        "events",
        "attempt",
        "attempts",
    }
)

#: The exact set this selector matches at HEAD (`e4dae942`). Pinned so a silent narrowing of
#: `_STEMS`, or a rename that drops a function out of it, is itself a test failure.
_EXPECTED_MATCHED_NAMES = frozenset(
    {
        "append_event",
        "insert_edges",
        "insert_symbols",
        "iter_attempts",
        "iter_edges",
        "iter_events",
        "iter_symbols",
        "record_attempt",
    }
)


def _name_matches(name: str) -> bool:
    tokens = name.lower().lstrip("_").split("_")
    return not _STEMS.isdisjoint(tokens)


def _annotation_denotes_list(node: ast.expr | None) -> bool:
    """True if `node` (a return-annotation AST node) denotes `list[...]`, bare `list`, or a
    `typing.List` spelling — at any depth of an `X | None` union or a subscript. `None` (no
    annotation at all) is handled separately by the caller, not here."""
    if node is None:
        return False
    if isinstance(node, ast.Name):
        return node.id in ("list", "List")
    if isinstance(node, ast.Attribute):
        return node.attr in ("list", "List")
    if isinstance(node, ast.Subscript):
        return _annotation_denotes_list(node.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _annotation_denotes_list(node.left) or _annotation_denotes_list(node.right)
    return False


def _matched_functions(
    source: str, *, filename: str = "<repository>"
) -> list[tuple[str, int, ast.expr | None]]:
    """Every `def`/`async def` node in `source` (module-level, class method, or nested closure)
    whose name matches `_STEMS`, as `(name, lineno, return_annotation_node_or_None)`."""
    tree = ast.parse(source, filename=filename)
    found: list[tuple[str, int, ast.expr | None]] = []

    class _Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._check(node)
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._check(node)
            self.generic_visit(node)

        def _check(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            if _name_matches(node.name):
                found.append((node.name, node.lineno, node.returns))

    _Visitor().visit(tree)
    return found


def _violations(source: str, *, filename: str = "<repository>") -> list[tuple[str, int, str]]:
    """`(name, lineno, annotation_text)` for every matched function that is list-returning, or
    that has no return annotation at all — unannotated defeats "return-type inspection" by
    construction, so it is reported rather than silently trusted."""
    out: list[tuple[str, int, str]] = []
    for name, lineno, ann in _matched_functions(source, filename=filename):
        if ann is None:
            out.append((name, lineno, "<unannotated>"))
        elif _annotation_denotes_list(ann):
            out.append((name, lineno, ast.unparse(ann)))
    return out


# --------------------------------------------------------------------------------------------
# 1. The real assertion, against the real file.
# --------------------------------------------------------------------------------------------


def test_no_symbols_edges_events_attempts_function_returns_a_list() -> None:
    source = _REPOSITORY_PY.read_text()
    matched = _matched_functions(source)
    # Guardrail 6: a check that walks zero functions "passes" vacuously and proves nothing.
    assert len(matched) >= 1, (
        "the symbol/edge/event/attempt name-stem selector matched NO function in "
        f"{_REPOSITORY_PY} -- this detector is examining nothing; a clean result from it would "
        "be meaningless. Investigate _STEMS or the file before trusting any verdict from this "
        "module."
    )
    violations = _violations(source)
    assert violations == [], (
        "docs/SPEC.md Sec 12 item 22: no state/repository.py function may return a `list` for "
        "symbols/edges/events/attempts -- a materialized list forces an unbounded result set "
        f"into memory, breaching the RSS ceilings Sec 12 item 22 exists to hold. Violations "
        f"(name, lineno, annotation): {violations}"
    )


def test_matched_function_set_is_exactly_the_known_streaming_and_write_surface() -> None:
    """Pins WHICH functions the selector currently examines. A silent narrowing of `_STEMS` (or
    a rename that drops a real function out of the matched set) is a test failure here, not a
    silent gap in the check above."""
    source = _REPOSITORY_PY.read_text()
    names = {name for name, _lineno, _ann in _matched_functions(source)}
    assert names == _EXPECTED_MATCHED_NAMES


# --------------------------------------------------------------------------------------------
# 2. Instrument validation (CLAUDE.md Guardrail 6): prove the detector actually discriminates,
#    on both sides, before trusting the clean result above.
# --------------------------------------------------------------------------------------------


def test_detector_fires_on_a_synthetic_list_bracket_violation() -> None:
    """Known-bad check, the exact shape research-17's own `"-> list\\["` grep already caught:
    a decoy function, name-matching the same stems the real functions use, declared
    `-> list[int]`, appended to a COPY of the real source (the file on disk is never touched)."""
    source = _REPOSITORY_PY.read_text()
    mutated = source + "\n\ndef iter_symbols_as_list(run_id: str) -> list[int]:\n    return []\n"
    violations = _violations(mutated, filename="<repository+decoy1>")
    flagged = {name for name, _lineno, _ann in violations}
    assert "iter_symbols_as_list" in flagged, (
        "the detector did not flag a synthetic `-> list[int]` function matching the stem set -- "
        "it cannot be trusted on the real file either"
    )


def test_detector_fires_on_a_bare_list_annotation() -> None:
    """A shape a literal `"-> list["` string search WOULD miss: a bare `list` with no generic
    parameter at all. research-17 named this explicitly as a gap in the narrow grep."""
    source = _REPOSITORY_PY.read_text()
    mutated = source + "\n\ndef record_attempt_list(row: object) -> list:\n    return []\n"
    violations = _violations(mutated, filename="<repository+decoy2>")
    flagged = {name for name, _lineno, _ann in violations}
    assert "record_attempt_list" in flagged


def test_detector_fires_on_a_list_inside_an_optional_union() -> None:
    """A second shape the narrow grep would miss: `list[...] | None`, a realistic return type
    for an optional collection."""
    source = _REPOSITORY_PY.read_text()
    decoy = "\n\ndef append_event_maybe(row: object) -> list[int] | None:\n    return None\n"
    mutated = source + decoy
    violations = _violations(mutated, filename="<repository+decoy3>")
    flagged = {name for name, _lineno, _ann in violations}
    assert "append_event_maybe" in flagged


def test_detector_is_silent_on_an_unrelated_ledger_function_despite_containing_edge() -> None:
    """Regression control for the false positive this test's OWN development hit: a plain
    substring search for "edge" also matches "ledger" (l-**edge**-r). `_name_matches` is
    token-based (splits on `_` and requires a WHOLE token match) specifically to exclude this.
    All three names below are real functions in `repository.py` today; none may be swept in."""
    assert not _name_matches("open_budget_ledger")
    assert not _name_matches("halt_budget_ledger")
    assert not _name_matches("_probe_repo_ledger")


def test_detector_is_silent_on_a_cosmetic_reflow_of_the_real_file() -> None:
    """Cosmetic control (Guardrail 6's fourth check): re-render the real file's own parse tree
    with `ast.unparse` -- identical AST structure, different literal text and whitespace, i.e. a
    reflow -- and confirm the verdict does not change. An instrument that turned red here would
    be asserting layout, not the return-type property the SPEC clause is actually about."""
    source = _REPOSITORY_PY.read_text()
    reflowed = ast.unparse(ast.parse(source))
    violations = _violations(reflowed, filename="<repository-reflowed>")
    assert violations == []
