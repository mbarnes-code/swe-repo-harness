"""The `blocked_by` writer claim, bound: the prose naming its writers, and the code that is them.

`RepoState.blocked_by`'s field description states **who writes this field**. It exists in two
byte-identical mirrors — `src/fleet/models/state.py::RepoState.blocked_by` and the same listing in
`docs/SPEC.md` §5.5 — and until this file **nothing bound either of them to the code**. Measured
across `tests/` at `a69fba8`: no test reads the description string, the §5.5 listing, or the writer
set, so *every* statement the description made about writers was unasserted.

That is not a hypothetical gap. `9b34497` was itself a correction of a false sentence in this
description, and **its own replacement text shipped two fresh false claims**, found and fixed at
`a69fba8`:

* it asserted *"there is no `StubState.ABANDONED`"* — `StubState.ABANDONED` exists and is the
  terminal member of that machine; the true fact the clause had been carrying (that `RepoStatus`
  has no `ABANDONED` member) was deleted in the same edit; and
* it asserted *"Four writers reach that rule"* flatly, when **two** of the four are live code and
  two are SPEC-mandated with zero producers — a count nobody had measured.

Both survived review, both shipped, and the tree was green throughout, because prose is only ever
compared to other prose here and **copies agree on a false sentence just as readily as a true one**.

So this module follows the method `tests/test_floor_rule_statements.py` established at `5f052f2`:
it **parses the claim out of the prose** and checks the parsed values against the code, so that
editing the prose changes what is asserted and nobody has to re-author an assertion by hand.

Six claims are parsed and six are checked:

1. **The live writer symbols.** The description names the non-delegating callers of the §3.5 append
   by symbol. Each parsed symbol must resolve to a real function, and the set must equal the writer
   set derived independently from the AST — **in both directions**, failing by `file:line`. A
   one-directional check is how this class regrows: prose naming a writer the code lacks and code
   writing `blocked_by` from a site the prose does not name are different defects and `9b34497`
   shipped the second. "Both directions" is only worth what the derived writer set is worth, and
   review lane CR4 measured two constructions in which that set did **not** move while the real
   writer set did — both because membership of the *append machinery* was inferred from a
   function's **file** and **name** rather than from its **body**. W20 repaired the derivation
   (`_qualname`, `_forwarded_name`, and the one-front cap in `_append_machinery`); the escapes it
   leaves are in the residual list below, not in this sentence.
2. **The definition of "non-delegating".** Review lane CR2 found the term was defined **nowhere in
   the tree**, and that under both of its natural readings the named pair is wrong: the
   non-delegating callers of `append_blocked_by` are `WaveScheduler.propagate_blocked` and
   `cli._quarantine_impl`, while the non-delegating callers of `propagate_blocked` are
   `runner._contain` alone. Only the derived reading — *call into the append or the rule fronting
   it, and be neither* — yields the pair. The description now states that definition; this module
   parses its entry points out and checks them against `_append_machinery()`, so the words and the
   code cannot drift apart in either direction.
3. **The enumeration, and any cardinal stated over it.** The hard count is **gone**, deliberately:
   "Four writers" was wrong when `9b34497` wrote it and *still* wrong after `a69fba8`, because
   `docs/SPEC.md` §3.5 routes a failed contract's descendants through the same propagation rule — a
   fifth trigger (CR2's M3, verified against §3.5's own paragraph). A cardinal is a second claim
   with its own maintenance cost that has now been wrong twice, so the enumeration carries the
   count. If one is ever reintroduced it must equal what the sentence enumerates.
4. **The contract trigger, required by `docs/SPEC.md` and not by this file.** The requirement is
   read out of §3.5 — it fires only while §3.5 says the scheduler applies the same rule on
   `contracts.status` — rather than hard-coded, because a hand-maintained expectation is the part
   that rots.
5. **The live / zero-producer split.** The description marks two triggers `LIVE` and the rest
   SPEC-mandated with an explicitly stated producer count. Both the split and that count are parsed;
   a naive check demanding a producer for every enumerated trigger would call the SPEC-mandated ones
   absent and push the prose back towards `9b34497`'s flat, unmeasured four.
6. **The enum existence claims.** Every `Class.MEMBER` the description asserts to exist, and every
   one it asserts *not* to exist, is resolved against `fleet.models.enums`. This is the check that
   fires on `9b34497`'s "there is no `StubState.ABANDONED`".

**What this binding still cannot catch, stated rather than implied:** a consistent rewrite of both
mirrors to one new *coherent* false sentence. Everything outside the six parsed claims is
unasserted — the §3.5 set-union semantics, the `OperatorQuarantine` consequence, the section
references, and the claim that a recompute "must not treat the writer set as closed" are prose this
module reads past; claim 4 checks that the contract trigger is *named*, never that what the sentence
says about it is true. It also cannot tell a claim that was **re-worded past an anchor** from one
that was **deleted**: the anchors below (`LIVE`, `non-delegating caller`, `Non-delegating means`,
`SPEC-mandated with N producers`) are natural-language phrases, and a rewrite that drops one fails
loudly, but the failure says "the description no longer states this", never "it states it
elsewhere in different words". Both residuals are the price of binding prose at all; neither is
closed here, and a reader must not mistake six bound claims for closure.

**Four further residuals, three of them W20's own.** (a) A *second* function in the sink file that
calls the append is refused, not classified: `_append_machinery` asserts rather than guessing which
of two identical-looking bodies is "the rule that fronts" and which is a writer of it, so a
legitimate second front cannot ship without a deliberate, reviewed edit to this module. That is a
worse day than silence and the only alternative to CR4's C1, where the guess was made silently and
wrongly. (b) `_forwarded_name` recognises only a *bare* pass-through; a wrapper that does one line
of bookkeeping before forwarding is reported as a writer. That direction is loud (the closure test
demands it be named) rather than silent, which is the direction to be wrong in. (c) The
contract-trigger requirement no longer lifts itself when `docs/SPEC.md` §3.5 stops mandating it —
it fails instead, because the self-lifting form was silenceable by one meaning-preserving word (see
that test's own docstring). (d) the producer-count test asserts "the description must say 0"; the
two closure tests pin `beyond_live` to 0, so it is not an independent producer measurement, and it
will block the correct prose edit on the day a SPEC-mandated trigger gains a producer. (e)
Pre-existing and not W20's, but measured while validating (d): `_code_writers` keys on
`<module>.<function>`, so two writers sharing a module and a name collapse to one entry — a second
one is invisible once the first is named. None of these is closed. This module binds six claims; it
does not certify the sentence.
"""

from __future__ import annotations

import ast
import enum
import inspect
import re
import textwrap
from pathlib import Path

import pytest

from fleet.models import enums as fleet_enums
from fleet.models.state import RepoState
from fleet.settings import FleetConfig

_ROOT = Path(__file__).resolve().parents[1]
_STATE = _ROOT / "src" / "fleet" / "models" / "state.py"
_SPEC = _ROOT / "docs" / "SPEC.md"
_SRC = _ROOT / "src" / "fleet"

#: The two mirrors of the listing. `docs/SPEC.md` §5.5 reproduces the model verbatim, so the field
#: description exists twice and a fix applied to one alone ships partial wording — the failure mode
#: CLAUDE.md records as "a multi-site correction split across authors or commits".
_MIRRORS: tuple[Path, ...] = (_STATE, _SPEC)

#: The field block, located by its annotated assignment rather than by line number.
#:
#: The `{0,3000}` bound is load-bearing, not decorative. An unbounded `[\s\S]*?` under this
#: terminator does not stop at the site it started in: `docs/SPEC.md` reproduces several models and
#: every one of them ends in a line that is exactly four spaces and a paren, so an unbounded match
#: starting at a missing terminator runs forward into the *next* model's and reports a block that
#: straddles two listings.
#:
#: **Measured, both mirrors, `704099c`: 2398 characters** — so the bound leaves **602 characters
#: (25% of the block)** of headroom, and the last two edits to this block spent +782 and +77. The
#: figure this comment carried until W20 ("~1.5 KB") was the size of the block `50ad1e4`
#: *replaced* (`a69fba8`: 1539); `50ad1e4` shipped 2321 and `68e539a` shipped 2398, so it was
#: false in the commit that wrote it and staler in the next — an unmeasured number inside the
#: module built to stop unmeasured numbers (CR4's I1). Overrun is loud, not silent: the
#: `match is not None` assert in `_prose` fires.
_FIELD_BLOCK = re.compile(
    r"^[ \t]*blocked_by:\s*list\[RepoId\]\s*=\s*Field\(\n[\s\S]{0,3000}?^[ \t]*\)$",
    re.MULTILINE,
)


def _normalise(text: str) -> str:
    """Collapse every run of whitespace.

    A reflow, a re-indent, or a re-wrap of the same words survives this; only a change of *words*
    does not. Layer-A of `tests/test_floor_rule_statements.py` promises the same thing for the same
    reason: an instrument that fails on a cosmetic edit is asserting layout, not meaning.
    """
    return " ".join(text.split())


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


class _Prose:
    """One mirror's `blocked_by` description, with the file and line to blame for it."""

    def __init__(self, path: Path, text: str, line: int) -> None:
        self.path = path
        self.text = text
        self.line = line

    @property
    def site(self) -> str:
        return f"{self.path.relative_to(_ROOT).as_posix()}:{self.line}"

    def __repr__(self) -> str:  # pragma: no cover - identifies the parametrised case
        return self.path.relative_to(_ROOT).as_posix()


def _prose(path: Path) -> _Prose:
    """The `blocked_by` description as a single normalised string, parsed out of `path`.

    Parsed with `ast`, never by string-slicing the source: the description is a run of adjacent
    string literals, so a re-wrap moves every seam. Parsing gives the *concatenated value* — the
    thing a reader of the model actually sees — and makes a reflow a no-op by construction.
    """
    source = path.read_text(encoding="utf-8")
    match = _FIELD_BLOCK.search(source)
    assert match is not None, (
        f"{path.relative_to(_ROOT).as_posix()} no longer contains a "
        f"`blocked_by: list[RepoId] = Field(...)` block. If the field was renamed or moved, update "
        f"_MIRRORS and _FIELD_BLOCK in the same change; if a mirror was deleted deliberately, say "
        f"so here rather than deleting this assertion."
    )
    assert "\n\n" not in match.group(0), (
        f"{path.relative_to(_ROOT).as_posix()}'s `blocked_by` block match spans a blank "
        f"line, so it has run past its own site and is describing something else"
    )
    node = ast.parse(textwrap.dedent(match.group(0))).body[0]
    assert isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Call)
    keyword = next((k for k in node.value.keywords if k.arg == "description"), None)
    assert keyword is not None, (
        f"{path.relative_to(_ROOT).as_posix()}'s `blocked_by` Field no longer carries a "
        f"`description=`. The claim this module binds would then be stated nowhere."
    )
    line = _line_of(source, match.start()) + keyword.value.lineno - 1
    return _Prose(path, _normalise(ast.literal_eval(keyword.value)), line)


def _mirrors() -> list[_Prose]:
    return [_prose(path) for path in _MIRRORS]


# ---------------------------------------------------------------------------------------------
# The code side: who actually writes `blocked_by`, derived from the AST and from nothing else
# ---------------------------------------------------------------------------------------------

#: The one statement that mutates the column. Everything called "a writer" is defined relative to
#: this, so that the writer set is *measured* rather than listed here — a hand-maintained list is
#: the part that rots (CLAUDE.md guardrail 6).
_SINK_SQL = re.compile(r"UPDATE\s+phases\s+SET\s+blocked_by", re.IGNORECASE)

_Func = tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef, tuple[str, ...]]


def _functions(path: Path, tree: ast.AST) -> list[_Func]:
    found: list[_Func] = []

    def walk(node: ast.AST, chain: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append((path, child, (*chain, child.name)))
                walk(child, (*chain, child.name))
            elif isinstance(child, ast.ClassDef):
                walk(child, (*chain, child.name))
            else:
                walk(child, chain)

    walk(tree, ())
    return found


def _string_literals(func: ast.AST) -> str:
    return " ".join(
        _normalise(node.value)
        for node in ast.walk(func)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def _callee_names(func: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Attribute):
                names.add(target.attr)
            elif isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _outermost(items: list[_Func]) -> list[_Func]:
    """Drop nested functions whose enclosing function is already in `items`.

    Not cosmetic. The sink's own body defines a closure (`unit`) that carries the SQL, and without
    this the closure's *name* joins the append-name set — after which every unrelated function in
    the tree that calls something called `unit` is reported as a `blocked_by` writer. Measured
    before this rule existed: `state/db.py::_run_in_immediate` was reported as a third writer.
    """
    keys = {(path, chain) for path, _, chain in items}
    return [
        (path, func, chain)
        for path, func, chain in items
        if not any((path, chain[:i]) in keys for i in range(1, len(chain)))
    ]


def _qualname(chain: tuple[str, ...]) -> str:
    """`Class.method` for a method, the bare name for a module-level function.

    The dotted form is what the description uses (`SqliteSchedulerStore.append_blocked_by`), and it
    is what turns the machinery exclusion below from a *name* match into an *identity* match. CR4's
    I4: a module-level `async def append_blocked_by(store, ...)` appended to `cli.py`, whose body
    called `store.propagate_blocked(...)` — an independent writer that merely shares a name, and
    explicitly not a wrapper — was excluded by the old bare-name filter and the suite stayed
    **18/18 green** over a closure sentence its existence falsified.
    """
    return ".".join(chain[-2:]) if len(chain) >= 2 else chain[-1]


def _forwarded_name(func: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """The attribute a bare pass-through forwards to, or `None` if this body is not one.

    Body-derived, which is the point. The description's exclusion clause says a caller is not a
    writer when it is "a same-named wrapper **forwarding to an inner store**"; until W20 the code
    said `name not in append_names`, which is a different rule, and
    `test_the_definition_the_prose_gives_is_the_one_derived_from_the_code` — the test named for
    exactly this drift — compared only the two entry-point names and so could not see it.

    A pass-through is: one statement after any docstring, and that statement is a call on an
    attribute, bare or awaited or returned. Deliberately strict — a wrapper that does one line of
    bookkeeping before forwarding is classified as a *writer*, which fails loudly (the closure test
    demands it be named) rather than silently widening the machinery.
    """
    body = list(func.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if len(body) != 1:
        return None
    statement = body[0]
    if not isinstance(statement, (ast.Return, ast.Expr)):
        return None
    value = statement.value
    if isinstance(value, ast.Await):
        value = value.value
    if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Attribute):
        return None
    return value.func.attr


def _append_machinery() -> tuple[dict[str, str], set[Path]]:
    """`({qualname: site}, files)` — what IS the §3.5 append, keyed by QUALIFIED name.

    Two members today: the store primitive carrying the `UPDATE`, and the rule that fronts it in
    the primitive's own module. Both are *derived*; neither is listed here. The field description
    names this pair in words, and
    `test_the_definition_the_prose_gives_is_the_one_derived_from_the_code` checks the words against
    this set — which is the whole of CR2's I1: before that, "non-delegating caller of the §3.5
    append" was a term defined nowhere in the tree, and under either of its two natural readings
    the pair the description names is wrong in both directions.

    **The fronting rule is capped at one, and the cap is the repair for CR4's C1.** The old
    derivation promoted into the machinery *every* function in a sink file that calls a sink name.
    That rule is filename-derived, and it composes into: a new caller of `append_blocked_by` that
    happens to live in `scheduler.py` is classified as part of the append rather than as a caller
    of it, so the closure test never sees it. CR4 demonstrated the consequence — a real
    `WaveScheduler.propagate_contract_blocked` writer plus the one repair the single failing test's
    own message recommends yields **18/18 green over a false closure sentence** — and W20
    reproduced it at `704099c` (16/2 for the writer alone, 18/0 once the definition sentence names
    it). Two sink-file callers have identical bodies, so nothing in the AST says which is "the
    rule that fronts" and which is a writer of it. This function therefore refuses to guess.
    """
    parsed = {
        path: ast.parse(path.read_text(encoding="utf-8")) for path in sorted(_SRC.rglob("*.py"))
    }
    every = [item for path, tree in parsed.items() for item in _functions(path, tree)]
    sinks = _outermost([item for item in every if _SINK_SQL.search(_string_literals(item[1]))])
    assert sinks, (
        "no function in `src/fleet` contains an `UPDATE phases SET blocked_by` statement. Either "
        "the write moved to a form this pattern cannot see — in which case this module is silently "
        "asserting nothing and must be repaired, not deleted — or the field is no longer written."
    )
    sink_names = {func.name for _, func, _ in sinks}
    sink_files = {path for path, _, _ in sinks}
    entry_points = {
        _qualname(chain): f"{path.relative_to(_ROOT).as_posix()}:{func.lineno}"
        for path, func, chain in sinks
    }
    fronts = _outermost(
        [
            item
            for item in every
            if item[0] in sink_files
            and item[1].name not in sink_names
            and _callee_names(item[1]) & sink_names
        ]
    )
    assert len(fronts) <= 1, (
        f"{len(fronts)} functions in "
        f"{sorted(p.relative_to(_ROOT).as_posix() for p in sink_files)} call the `blocked_by` "
        f"append sink without being it: "
        f"{', '.join(sorted(_qualname(chain) for _p, _f, chain in fronts))}. At most one of them "
        f"is 'the rule that fronts the append'; the rest are non-delegating CALLERS of it, and "
        f"their bodies are indistinguishable, so this module will not guess. Adjudicate it HERE, "
        f"in this file, with your reason. Do NOT resolve it by adding the new name to the "
        f"description's definition sentence: that is CR4's C1 exactly — a function promoted into "
        f"the machinery is thereafter invisible to "
        f"`test_every_code_site_that_writes_blocked_by_is_named_by_the_prose`, and the measured "
        f"result of that repair is 18/18 green over a closure claim the new writer makes false."
    )
    for path, func, chain in fronts:
        entry_points[_qualname(chain)] = f"{path.relative_to(_ROOT).as_posix()}:{func.lineno}"
    return entry_points, sink_files


def _code_writers() -> dict[str, str]:
    """`{"<module>.<function>": "<relpath>:<line>"}` for every site in `src/` that writes the field.

    Three by-rule exclusions, none of them a list of names, and the first two are the two halves
    of the description's own definition rather than one filter standing in for both:

    * **The append itself is not a caller of the append** — matched by QUALIFIED name, so it
      excludes `SqliteSchedulerStore.append_blocked_by` and `WaveScheduler.propagate_blocked` and
      nothing else. A bare-name match here was CR4's I4: any function anywhere called
      `append_blocked_by` was excluded whatever its body did.
    * **A same-named wrapper forwarding to an inner store is not a caller either** — matched by
      BODY, via `_forwarded_name`, so the `_ScanWaveStore`/`_ScopedWaveStore` pass-throughs are
      excluded because of what they do, not because of what they are called. The `Protocol`
      declaration never reaches either rule: its body calls nothing.
    * **A nested closure is not a separate site**, per `_outermost`.
    """
    entry_points, _ = _append_machinery()
    call_names = {qualname.rpartition(".")[2] for qualname in entry_points}
    parsed = {
        path: ast.parse(path.read_text(encoding="utf-8")) for path in sorted(_SRC.rglob("*.py"))
    }
    every = [item for path, tree in parsed.items() for item in _functions(path, tree)]
    writers = _outermost(
        [
            item
            for item in every
            if _callee_names(item[1]) & call_names
            and _qualname(item[2]) not in entry_points
            and not (item[1].name in call_names and _forwarded_name(item[1]) == item[1].name)
        ]
    )
    return {
        f"{path.stem}.{func.name}": f"{path.relative_to(_ROOT).as_posix()}:{func.lineno}"
        for path, func, _ in writers
    }


# ---------------------------------------------------------------------------------------------
# The parse: every value below is read OUT of the prose, never expected of it
# ---------------------------------------------------------------------------------------------

_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}

#: Normalised text has exactly one space per whitespace run, so a sentence boundary is a terminator
#: followed by that single space. `:` and `;` are terminators here because the description states
#: the count and its enumeration as one colon-joined sentence.
_SENTENCE_BREAK = re.compile(r"(?<=[.;:])\s")

#: "Four writers reach that rule". The count and the noun, so a bare numeral elsewhere is not it.
_WRITER_COUNT = re.compile(
    r"\b(" + "|".join(_NUMBER_WORDS) + r"|\d+)\s+writers?\s+reach\b", re.IGNORECASE
)

#: **Uppercase, deliberately.** Lowercase "live" is an ordinary English word that appears in
#: ordinary prose about code; the description uses the shout form to mark the split, and matching
#: case-insensitively would let an unrelated sentence supply the anchor.
_LIVE_MARKER = re.compile(r"\bLIVE\b")

#: The clause that names the writers by symbol. This phrase is what makes the symbol list a *claim*
#: about closure ("the only ...") rather than a list of examples.
_CALLER_MARKER = re.compile(r"non-delegating callers?", re.IGNORECASE)

#: The zero-producer clause, with its producer count parsed out rather than assumed to be zero.
_ZERO_PRODUCER = re.compile(r"SPEC-mandated with (\d+) producers?", re.IGNORECASE)

#: A dotted symbol in the code's own register: `runner._contain`, `cli._quarantine_impl`. The
#: leading lowercase is what keeps `RepoStatus.REQUIRES_HUMAN_INTERVENTION` out of the symbol set —
#: enum members are claims of a different kind and are checked by their own test below.
_SYMBOL = re.compile(r"\b([a-z][a-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b")

#: A parenthesised SPEC reference, which is how each writer in the enumeration is identified.
_SECTION_REF = re.compile(r"\(§\d+(?:\.\d+)*\)")

#: An existence claim, negated. Two forms, because the description states one of each: "RepoStatus
#: has no ABANDONED member" and (in `9b34497`, the defect) "there is no StubState.ABANDONED".
_NO_MEMBER = re.compile(r"\b([A-Z][A-Za-z]+)\s+has no\s+`?([A-Z][A-Z_]{2,})`?\s+member")
_NO_DOTTED = re.compile(r"\bthere is no\s+`?([A-Z][A-Za-z]+)\.([A-Z][A-Z_]{2,})`?")

#: An existence claim, positive: any `Class.MEMBER` naming a class `fleet.models.enums` defines.
_DOTTED_MEMBER = re.compile(r"\b([A-Z][A-Za-z]+)\.([A-Z][A-Z_]{2,})\b")

#: The annotation the description cites when it argues the SQL column and this field are not
#: interchangeable. Parsed so that a rewrite citing a *different* annotation is checked, not copied.
_CITED_ANNOTATION = re.compile(r"this `([^`]+)` annotation would reject it")

#: The settings path the description cites for the §3.4 zero-producer writer. Parsed and resolved:
#: it is the only handle either zero-producer writer offers on the code, and without it that half of
#: the split is bound by arithmetic alone.
_CITED_SETTING = re.compile(r"`([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)` breach")

#: The sentence that DEFINES the key term, which must be a different sentence from the one that
#: uses it — `_sentence_with` requires a unique match, and a definition folded into the claim it
#: defines would make both anchors ambiguous.
_DEFINITION_MARKER = re.compile(r"Non-delegating means")

#: The module this description points a reader at for the derived definition of its key term.
_CITED_TEST = re.compile(r"`(tests/[A-Za-z0-9_./-]+\.py)`")

#: `Class.method` in the definition sentence: the entry points that ARE the §3.5 append.
_ENTRY_POINT = re.compile(r"`([A-Z][A-Za-z0-9_]*)\.([a-z_][A-Za-z0-9_]*)`")

#: `docs/SPEC.md` §3.5's own heading. The contract mandate is read out of THAT section rather
#: than out of the whole file, so a sentence about contracts somewhere else cannot supply it.
_SPEC_SECTION = re.compile(r"^###[ \t]+3\.5[ \t]", re.MULTILINE)

#: Any ATX heading, used only to find where §3.5 ends.
_SPEC_HEADING = re.compile(r"^#{1,6}[ \t]", re.MULTILINE)

#: How the description must identify that trigger, if §3.5 mandates it.
_CONTRACT_TRIGGER = re.compile(r"contracts\.status='FAILED'")

#: The column §3.5's contract paragraph says the `contract_id` is appended to. Paired with
#: `_CONTRACT_TRIGGER` because the trigger token ALONE is not specific enough: §3.5 states it twice
#: — once in the ADR-0019 rollback paragraph, which says nothing about propagation. W20 measured
#: that: removing the trigger token from the mandate sentence and searching §3.5 for the trigger
#: alone still passes 18/18, because the rollback paragraph supplies it. Both anchors are
#: code-quoted identifiers, so the pair survives any rewording of the sentence carrying them.
_SPEC_BLOCKED_BY_COLUMN = re.compile(r"phases\.blocked_by")


def _spec_section_3_5() -> str:
    """`docs/SPEC.md` §3.5's text, or a loud failure — never an empty string.

    Slicing by heading rather than searching the whole file keeps "§3.5 mandates this" honest, and
    a heading that no longer resolves fails here instead of quietly yielding "" — the difference
    `_sentence_with` states below, applied to the SPEC side, which is where CR4's I2 found it
    missing.
    """
    spec = _SPEC.read_text(encoding="utf-8")
    heads = _SPEC_SECTION.findall(spec)
    assert len(heads) == 1, (
        f"docs/SPEC.md has {len(heads)} `### 3.5 ` heading(s), expected exactly 1. The contract "
        f"mandate this module reads is section-scoped; if §3.5 was renumbered or split, move "
        f"_SPEC_SECTION in the same change rather than letting the scope silently become the "
        f"whole file or nothing."
    )
    start = _SPEC_SECTION.search(spec)
    assert start is not None
    following = _SPEC_HEADING.search(spec, start.end())
    return spec[start.start() : following.start() if following else len(spec)]


def _spec_contract_mandate() -> str | None:
    """§3.5's paragraph mandating the contract propagation, or `None` if §3.5 no longer states it.

    Identified by the two code tokens the mandate is made of — the trigger and the column it writes
    — inside one blank-line-separated paragraph of §3.5, rather than by the prose sentence that
    joins them. That is the repair for CR4's I2: the old anchor was the phrase "the scheduler
    applies the same propagation rule", so inserting one meaning-preserving word ("the **very**
    same") took the whole check to a silent skip.
    """
    paragraphs = [
        block
        for block in _spec_section_3_5().split("\n\n")
        if _CONTRACT_TRIGGER.search(block) and _SPEC_BLOCKED_BY_COLUMN.search(block)
    ]
    assert len(paragraphs) <= 1, (
        f"docs/SPEC.md §3.5 has {len(paragraphs)} paragraphs naming both "
        f"`contracts.status='FAILED'` and `phases.blocked_by`, so 'the' contract mandate is "
        f"ambiguous. Narrow the anchors in the same change rather than letting this test read one "
        f"paragraph and a reader read another."
    )
    return paragraphs[0] if paragraphs else None


def _sentence_with(prose: _Prose, marker: re.Pattern[str], what: str) -> str:
    """The one sentence of `prose` carrying `marker`, or a loud failure naming the site.

    Failing loudly on a missing anchor rather than returning "" is the whole difference between an
    instrument and a decoration: an empty result compares equal to an empty expectation and passes
    vacuously, which is how a detector reports clean on the state it exists to catch.
    """
    hits = [s for s in _SENTENCE_BREAK.split(prose.text) if marker.search(s)]
    assert len(hits) == 1, (
        f"{prose.site}: the `blocked_by` description states {what} in {len(hits)} sentence(s), "
        f"expected exactly 1 (anchor {marker.pattern!r}). This module cannot tell a claim "
        f"re-worded past its anchor from one deleted; if the wording changed deliberately, move "
        f"the anchor in the same change rather than removing the check."
    )
    return hits[0]


def _prose_live_symbols(prose: _Prose) -> set[str]:
    sentence = _sentence_with(prose, _CALLER_MARKER, "which writers are live, by symbol")
    return {f"{module}.{name}" for module, name in _SYMBOL.findall(sentence)}


def _enum_classes() -> dict[str, type[enum.Enum]]:
    """The enum classes `fleet.models.enums` *defines*, not the ones it imports.

    `StrEnum`/`IntEnum` are in that module's namespace and are `Enum` subclasses; admitting them
    would let `SomeAlias.MEMBER` resolve against a stdlib base and pass.
    """
    return {
        name: obj
        for name, obj in vars(fleet_enums).items()
        if inspect.isclass(obj)
        and issubclass(obj, enum.Enum)
        and obj.__module__ == fleet_enums.__name__
    }


def _existence_claims(prose: _Prose) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """`(asserted_to_exist, asserted_absent)` — every enum claim the description makes.

    Negated spans are removed from the text *before* the positive scan, because the two forms
    overlap: "there is no StubState.ABANDONED" contains a perfectly good-looking `Class.MEMBER`, and
    a positive-only scan reads `9b34497`'s false claim as an assertion that the member exists — i.e.
    it agrees with the defect.
    """
    classes = _enum_classes()
    absent: set[tuple[str, str]] = set()
    remainder = prose.text
    for pattern in (_NO_MEMBER, _NO_DOTTED):
        for match in list(pattern.finditer(remainder)):
            absent.add((match.group(1), match.group(2)))
        remainder = pattern.sub(" ", remainder)
    exists = {(cls, member) for cls, member in _DOTTED_MEMBER.findall(remainder) if cls in classes}
    return exists, absent


# ---------------------------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def code_writers() -> dict[str, str]:
    return _code_writers()


@pytest.mark.parametrize("prose", _mirrors(), ids=lambda p: repr(p))
def test_the_prose_the_two_mirrors_carry_is_the_value_the_model_exposes(prose: _Prose) -> None:
    """Both mirrors state one claim, and the state.py copy is the description Pydantic serves.

    The mirror check is textual and therefore proves only that the copies agree — which is exactly
    the property `9b34497` had while being false, and is why the rest of this module exists. What it
    *does* buy: a correction applied to one mirror alone fails here instead of shipping.

    The runtime cross-check is the guardrail-6 half: a declaration read is not a value exercised, so
    the text this module parses is asserted to be the string `RepoState.model_fields` actually
    hands a reader, not merely a string that appears in the file.
    """
    reference = _normalise(RepoState.model_fields["blocked_by"].description or "")
    assert reference, "`RepoState.blocked_by` exposes no description at runtime"
    assert prose.text == reference, (
        f"{prose.site}: this mirror's `blocked_by` description differs from the one "
        f"`RepoState.model_fields` exposes. A correction reached one copy and not the other."
    )


@pytest.mark.parametrize("prose", _mirrors(), ids=lambda p: repr(p))
def test_every_live_writer_the_prose_names_resolves_and_actually_writes_blocked_by(
    prose: _Prose, code_writers: dict[str, str]
) -> None:
    """Prose → code. Each symbol the description names is resolved and must be a real writer.

    Resolution is separate from writer-ness on purpose: "this symbol does not exist" and "this
    symbol exists but does not write the field" are different defects and deserve different
    failures.
    """
    named = _prose_live_symbols(prose)
    assert named, (
        f"{prose.site}: the description names no live writer symbol at all. Before `a69fba8` it "
        f"named none either, and the count it stated was true of nothing."
    )
    for symbol in sorted(named):
        module, _, function = symbol.partition(".")
        candidates = list(_SRC.rglob(f"{module}.py"))
        assert candidates, (
            f"{prose.site}: names `{symbol}`, but `src/fleet/**/{module}.py` does not exist"
        )
        defined = any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function
            for path in candidates
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
        assert defined, (
            f"{prose.site}: names `{symbol}` as a live writer, but no function `{function}` is "
            f"defined in {[str(p.relative_to(_ROOT)) for p in candidates]}"
        )
        assert symbol in code_writers, (
            f"{prose.site}: names `{symbol}` as a non-delegating caller of the §3.5 append, "
            f"but the AST finds no call to that append in it. Writers measured: "
            f"{sorted(code_writers.items())}"
        )


@pytest.mark.parametrize("prose", _mirrors(), ids=lambda p: repr(p))
def test_every_code_site_that_writes_blocked_by_is_named_by_the_prose(
    prose: _Prose, code_writers: dict[str, str]
) -> None:
    """Code → prose, the direction `9b34497` failed in.

    The description's symbol list is a closure claim ("the only non-delegating callers"), so a
    writer added to `src/` without a matching edit here makes the description false the moment it
    lands. Reported by the *code* site's `file:line`, because that is the thing to go and look at.
    """
    named = _prose_live_symbols(prose)
    unnamed = {symbol: site for symbol, site in code_writers.items() if symbol not in named}
    assert not unnamed, (
        f"{prose.site} claims to name every non-delegating caller of the §3.5 `blocked_by` append, "
        f"but these write it and are not named: "
        f"{', '.join(f'{sym} at {site}' for sym, site in sorted(unnamed.items()))}. Either name "
        f"them in BOTH mirrors, or the closure claim is false."
    )


@pytest.mark.parametrize("prose", _mirrors(), ids=lambda p: repr(p))
def test_the_enumeration_carries_the_count_and_any_stated_cardinal_agrees_with_it(
    prose: _Prose,
) -> None:
    """The enumeration is what is asserted; a cardinal, if one is stated, must agree with it.

    The description no longer states a cardinal, deliberately: "Four writers" was wrong when
    `9b34497` wrote it (two of the four had zero producers) and was **still** wrong after `a69fba8`
    corrected the rest of the sentence, because `docs/SPEC.md` §3.5 routes a failed contract's
    descendants through the same propagation rule — a fifth trigger the count never included. A
    count is a second claim with its own maintenance cost and it has now been wrong twice, so the
    enumeration carries it.

    This test therefore asserts two things and not a number: that every writer the description marks
    LIVE is matched by a symbol (a LIVE writer with no symbol is the unmeasured form `9b34497`
    shipped), and that **if** a cardinal is reintroduced it equals what the sentence enumerates. The
    conditional half is what stops the retired defect from simply being retyped.
    """
    live_sentence = _sentence_with(prose, _LIVE_MARKER, "which writers are LIVE")
    zero_sentence = _sentence_with(prose, _ZERO_PRODUCER, "which writers have no producers")
    live = len(_SECTION_REF.findall(live_sentence))
    zero = len(_SECTION_REF.findall(zero_sentence))
    assert live and zero, (
        f"{prose.site}: the enumeration identifies {live} LIVE and {zero} SPEC-mandated trigger(s) "
        f"by parenthesised SPEC reference. With the cardinal gone the references ARE the "
        f"enumeration, so an empty one asserts nothing."
    )
    assert live == len(_prose_live_symbols(prose)), (
        f"{prose.site}: marks {live} writer(s) LIVE but names "
        f"{len(_prose_live_symbols(prose))} symbol(s) for them. A LIVE writer with no symbol "
        f"is the unmeasured form `9b34497` shipped."
    )
    cardinal = _WRITER_COUNT.search(prose.text)
    if cardinal is not None:
        word = cardinal.group(1).lower()
        stated = _NUMBER_WORDS.get(word) or int(word)
        assert stated == live + zero, (
            f"{prose.site}: states {stated} writers but enumerates {live} LIVE ({live_sentence!r}) "
            f"and {zero} SPEC-mandated ({zero_sentence!r}) = {live + zero}. This is the claim the "
            f"description dropped for exactly this reason; if you are reintroducing it, measure it."
        )


@pytest.mark.parametrize("prose", _mirrors(), ids=lambda p: repr(p))
def test_the_definition_the_prose_gives_is_the_one_derived_from_the_code(prose: _Prose) -> None:
    """CR2's I1: the description's key term must be self-defining, and defined the way the code is.

    "the only non-delegating callers of the §3.5 append" was, until this commit, a term defined
    **nowhere in the tree**, and under both of its natural readings the pair it names is wrong:

    * callers of `append_blocked_by` are `WaveScheduler.propagate_blocked` and
      `cli._quarantine_impl` — so `runner._contain` is a non-member and `propagate_blocked` is an
      omitted member;
    * callers of `propagate_blocked` are `runner._contain` alone — so `cli._quarantine_impl` is the
      non-member.

    Only the derived reading — call into the append *or the rule that fronts it*, and be neither —
    yields the named pair. So the description now states that definition, and this test parses the
    entry points out of it and checks them against `_append_machinery()`. Nobody can loosen the
    prose definition without the assertion changing with it, and nobody can restructure the append
    without the prose failing.
    """
    sentence = _sentence_with(prose, _DEFINITION_MARKER, "what 'non-delegating' means")
    named = {f"{cls}.{method}" for cls, method in _ENTRY_POINT.findall(sentence)}
    derived, sink_files = _append_machinery()
    assert named == set(derived), (
        f"{prose.site}: defines 'non-delegating' against {sorted(named)}, but the append machinery "
        f"derived from the `phases.blocked_by` write sink is {sorted(derived)}. The term and "
        f"the code have drifted; whichever moved, they must move together. The comparison is on "
        f"`Class.method`, not on the method name alone: a bare-name comparison passes when the "
        f"prose attributes the append to the wrong class."
    )
    for cls, method in _ENTRY_POINT.findall(sentence):
        resolved = any(
            isinstance(node, ast.ClassDef)
            and node.name == cls
            and any(
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == method
                for child in ast.walk(node)
            )
            for path in sink_files
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
        assert resolved, (
            f"{prose.site}: names `{cls}.{method}` as an entry point of the §3.5 append, but no "
            f"such method is defined in {[p.name for p in sink_files]}"
        )

    cited = _CITED_TEST.search(prose.text)
    assert cited is not None, (
        f"{prose.site}: no longer points a reader at the module that enforces this definition "
        f"(anchor {_CITED_TEST.pattern!r}). A derived definition that lives only in a test nobody "
        f"is sent to is what CR2 objected to."
    )
    assert (_ROOT / cited.group(1)).exists(), (
        f"{prose.site}: points at `{cited.group(1)}`, which does not exist"
    )
    assert cited.group(1) == Path(__file__).relative_to(_ROOT).as_posix(), (
        f"{prose.site}: points at `{cited.group(1)}`, but the module deriving the definition is "
        f"{Path(__file__).relative_to(_ROOT).as_posix()}"
    )


@pytest.mark.parametrize("prose", _mirrors(), ids=lambda p: repr(p))
def test_the_contract_trigger_spec_3_5_mandates_is_in_the_enumeration(prose: _Prose) -> None:
    """CR2's M3: §3.5 routes failed contracts through the same rule, so they are in the enumeration.

    The requirement is read out of `docs/SPEC.md` rather than asserted here: it fires only while
    §3.5 actually says the scheduler applies the same propagation rule on `contracts.status`. That
    matters because the alternative — hard-coding "the enumeration must mention contracts" — is the
    hand-maintained expectation that rots the moment the SPEC changes, and this module exists
    because hand-maintained agreement already failed twice on this sentence.

    **This check used to `pytest.skip` when its SPEC-side anchor did not match, and the anchor was
    the prose phrase "the scheduler applies the same propagation rule".** CR4 measured what that
    costs: dropping the trigger from the description in both mirrors AND inserting one
    meaning-preserving word into §3.5 ("the **very** same propagation rule") reports
    **16 pass / 2 skip / 0 fail** — `a69fba8`'s exact defect, green in CI, because nobody reads a
    skip. So the anchor is now the pair of code tokens the mandate is made of,
    `contracts.status='FAILED'` and `phases.blocked_by`, required in **one paragraph of §3.5** —
    they survive any rewording of the sentence joining them — and their absence **fails**. The
    trigger token alone was measured insufficient: §3.5 states it twice, and the other occurrence
    (the ADR-0019 rollback paragraph) is not about propagation at all, so removing the token from
    the mandate sentence still left a single-token anchor passing 18/18.

    The trade that buys, stated rather than implied: the requirement no longer lifts by itself. If
    §3.5 genuinely stops mandating the contract trigger, this test fails and someone must delete it
    and the description's clause together. That is a worse day than a skip and a better one than
    `a69fba8` shipping twice; a requirement that can lift itself is a requirement one word can
    silence.

    What it still cannot do: it checks the trigger is *named in the zero-producer clause*, not that
    the clause's description of it is true. That is residual 1 of the module docstring in a new
    place.
    """
    assert _spec_contract_mandate() is not None, (
        f"docs/SPEC.md §3.5 has no paragraph naming both `contracts.status='FAILED'` and "
        f"`phases.blocked_by` (anchors {_CONTRACT_TRIGGER.pattern!r} and "
        f"{_SPEC_BLOCKED_BY_COLUMN.pattern!r}), so the mandate this test derives from the primary "
        f"source cannot be read there. Either the SPEC dropped the mandate — in which case delete "
        f"this test AND the description's contract clause in the same change, in both mirrors — "
        f"or it moved out of §3.5 and _SPEC_SECTION must move with it. This is deliberately not a "
        f"skip: the skip it replaces was reachable by one meaning-preserving word."
    )
    sentence = _sentence_with(prose, _ZERO_PRODUCER, "which writers have no producers")
    assert _CONTRACT_TRIGGER.search(sentence), (
        f"{prose.site}: `docs/SPEC.md` §3.5 states the scheduler applies the same propagation rule "
        f"on `contracts.status='FAILED'`, appending the contract_id to `phases.blocked_by` — so a "
        f"failed contract's descendants are a trigger reaching the rule. The enumeration does not "
        f"name it: {sentence!r}. This is the omission that made the retired cardinal wrong."
    )


@pytest.mark.parametrize("prose", _mirrors(), ids=lambda p: repr(p))
def test_the_spec_mandated_writers_have_the_producer_count_the_prose_states(
    prose: _Prose, code_writers: dict[str, str]
) -> None:
    """The zero-producer half of the split: **the description must state 0, and 0 is measured.**

    A check that demanded a live producer for every enumerated writer would call these two absent
    and push the description back to a flat four — so the split is what makes the binding possible
    at all. The producer count is *parsed*, not expected, and it is compared against a measurement:
    the writers the AST finds, minus the ones the description marks LIVE.

    **What that comparison can and cannot do, corrected.** This docstring used to say "if the
    description ever says '1 producer', that must show up as a code writer beyond the ones it marks
    LIVE". CR4 showed that capability is unreachable in any tree state, and W20 reproduced it at
    `704099c`. The two closure tests above make `code_writers` and the named symbol set equal
    whenever they are green, so `beyond_live` is pinned at 0 and only `stated_producers == 0` can
    pass. Injecting a real third writer and stating "1 producer" satisfies neither configuration:
    leave it unnamed and this test passes (1 == 3-2) while the closure test fails; name it and this
    test fails (0 != 1) alongside the LIVE-count test. So the assertion is
    **"the description must say 0"** — true, load-bearing and independently reachable (stating "1"
    with the code untouched fails here and nowhere else; W20 measured that as the only failure),
    but it is not the independent producer measurement the old wording promised, and Rule 12's
    redundancy question is what settled keeping it rather than deleting it.

    **The cost, disclosed.** On the day one of the three SPEC-mandated triggers gains a real
    producer, the *correct* prose edit is blocked by this test rather than validated by it: the
    closure test will require the new writer to be named in the "non-delegating callers" sentence,
    and `_prose_live_symbols` reads that same sentence, so the new writer counts as LIVE and
    `beyond_live` returns to 0. Fixing that needs the description to separate "named as a caller"
    from "marked LIVE" — today one sentence carries both anchors — which is a prose change, not a
    change here.
    """
    sentence = _sentence_with(prose, _ZERO_PRODUCER, "which writers have no producers")
    stated_producers = int(_ZERO_PRODUCER.search(sentence).group(1))  # type: ignore[union-attr]
    beyond_live = len(code_writers) - len(_prose_live_symbols(prose))
    assert stated_producers == beyond_live, (
        f"{prose.site}: says the SPEC-mandated writers have {stated_producers} producer(s), but "
        f"the AST finds {len(code_writers)} writer(s) in `src/` and the description marks "
        f"{len(_prose_live_symbols(prose))} of them LIVE, leaving {beyond_live}."
    )


@pytest.mark.parametrize("prose", _mirrors(), ids=lambda p: repr(p))
def test_every_enum_existence_claim_matches_models_enums(prose: _Prose) -> None:
    """The check that fires on `9b34497`'s "there is no StubState.ABANDONED".

    Both directions, because the defect was a *pair*: the false clause asserted absence of a member
    that exists, and in doing so deleted the true clause asserting absence of one that does not
    (`RepoStatus.ABANDONED`). An instrument reading only positive claims agrees with the first and
    is blind to the second.
    """
    classes = _enum_classes()
    exists, absent = _existence_claims(prose)
    assert exists or absent, (
        f"{prose.site}: the description makes no enum existence claim at all. It carried two "
        f"before `a69fba8`; if they were removed deliberately, remove this check in the same "
        f"change."
    )
    for cls, member in sorted(exists):
        assert member in classes[cls].__members__, (
            f"{prose.site}: names `{cls}.{member}`, but `{cls}` has members "
            f"{sorted(classes[cls].__members__)}"
        )
    for cls, member in sorted(absent):
        assert cls in classes, (
            f"{prose.site}: asserts `{cls}` has no `{member}`, but `fleet.models.enums` defines no "
            f"`{cls}` at all — the disambiguation is about a class that does not exist"
        )
        assert member not in classes[cls].__members__, (
            f"{prose.site}: asserts `{cls}.{member}` does not exist, but it does — "
            f"`{cls}` has members {sorted(classes[cls].__members__)}. This is `9b34497`'s "
            f"defect: a correction commit turned a true disambiguation into a false existence "
            f"claim."
        )


@pytest.mark.parametrize("prose", _mirrors(), ids=lambda p: repr(p))
def test_the_annotation_and_the_setting_the_prose_cites_are_the_ones_the_code_carries(
    prose: _Prose,
) -> None:
    """The two remaining parsed values: the field's annotation, and the §3.4 writer's setting path.

    The annotation is the whole argument for "the SQL column and this field are not interchangeable"
    — if it were widened, the argument would be false while the sentence stayed the same. The
    setting path is the only handle either SPEC-mandated writer offers on the code; without it that
    half of the split is bound by arithmetic alone.
    """
    cited = _CITED_ANNOTATION.search(prose.text)
    assert cited is not None, (
        f"{prose.site}: no longer cites the annotation that makes the SQL column and this field "
        f"non-interchangeable (anchor {_CITED_ANNOTATION.pattern!r})"
    )
    source = ast.parse(_STATE.read_text(encoding="utf-8"))
    annotation = next(
        ast.unparse(node.annotation)
        for cls in ast.walk(source)
        if isinstance(cls, ast.ClassDef) and cls.name == "RepoState"
        for node in cls.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "blocked_by"
    )
    assert cited.group(1) == annotation, (
        f"{prose.site}: cites `{cited.group(1)}` as the annotation that would reject a "
        f"`contract_id`, but the field is annotated `{annotation}`"
    )

    setting = _CITED_SETTING.search(prose.text)
    assert setting is not None, (
        f"{prose.site}: no longer cites a settings path for the §3.4 writer "
        f"(anchor {_CITED_SETTING.pattern!r})"
    )
    model: type = FleetConfig
    for part in setting.group(1).split("."):
        fields = getattr(model, "model_fields", {})
        assert part in fields, (
            f"{prose.site}: cites `{setting.group(1)}`, but `{model.__name__}` has no field "
            f"`{part}`. The SPEC-mandated §3.4 writer is named after a setting that does not exist."
        )
        model = fields[part].annotation  # type: ignore[assignment]
