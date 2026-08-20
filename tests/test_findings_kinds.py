"""The `findings.kind` vocabulary: emitters in `src/` vs. the two curated listings.

`schema.sql` deliberately declares `findings.kind` as FREE TEXT, so nothing in the database
constrains it and the only record of the vocabulary is a comment block — duplicated verbatim in
`docs/SPEC.md`. Both copies are hand-maintained, and both drifted: a sweep at `8ea1881` found
**19** kinds emitted by live writers that `schema.sql` never listed and **21** that `docs/SPEC.md`
never listed, in a listing whose most recent review had reported exactly one. This module is what
makes that check mechanical instead of periodic.

Three properties, in the order a drift is most likely to appear:

1. **The two copies agree.** Pure text extraction, no heuristics. The `CapabilityDrift` /
   `BackendUnavailable` pair was in `schema.sql` and not in the SPEC for several rounds.
2. **Every kind an emitter can write is listed** (emitted-but-unlisted), and every name under the
   listing's EMITTED heading still has a live writer (listed-but-no-longer-emitted). The
   `Shipped:`/DECLARED half is deliberately NOT bound in the second direction: its own CAVEAT says
   several of its names are read by Python that nothing writes, which is a stated property of that
   half of the listing, not a defect.
3. **Every findings writer in `src/` is one the extractor resolved.** This is the load-bearing one.
   An extractor that silently fails to see a new emitter reports "no drift" forever, which is the
   failure mode that produced the 19-kind gap in the first place. So an `INSERT INTO findings`
   whose `kind` this module cannot resolve to a literal is a hard failure naming the site, unless
   the site is declared in `_INDIRECT_SITES` below with the channel that supplies it. That gate is
   only as good as the step in front of it: a site the AST walk never *recognises* is never handed
   to it and vanishes without a sound. So recognition is not trusted on its own. `_recognition_gap`
   re-derives the sites from the source **text** of every module — the parse is used only to
   subtract prose (docstrings, comments), never to find a site — and fails naming file and line
   when the two instruments disagree in either direction: a text site the walk never saw, or a
   walk site the text scan cannot find.

What this does NOT catch, stated rather than implied, worst first:

* **SQL whose source text never spells the phrase**, because both instruments read source text and
  are therefore blind to the same forms: `f"INSERT INTO {table}"`, a table name split across
  concatenated fragments (`"...fin" "dings..."`), a query builder, or a statement loaded from a
  file. The text cross-check is an independent *derivation*, not an independent *language*, so it
  narrows the recognition residual without eliminating it. A `FindingKind` enum every writer had
  to go through is what would remove it — a change to the **16** findings-INSERT call sites in
  `src/fleet` (**13** of them in `cli.py`, 2 in `orchestrator/findings.py`, 1 in
  `state/repository.py`; the "16" is the whole-`src/` count, not `cli.py`'s), not to this test.
* **A recognised site whose `kind` goes through a form the resolver accepts and mis-resolves.**
  Every form the resolver does not understand returns "unresolved" and trips property 3, and a
  name assigned more than once now resolves to the union of its assignments rather than to
  whichever one `ast.walk` reached first (and a params *tuple* reached through such a name is
  treated as not understood, which is louder still) — so this is narrow, but it is not zero.
* **`_recognition_gap` matches text hits to recognised literals by line, not by column.** Two
  distinct findings INSERTs beginning on one physical line, only one of them recognised, would
  read as agreement. No such line exists or can exist under the 100-column limit these statements
  already exceed, but the imprecision is real and is stated rather than assumed away.
* **The worker channel is a narrower instrument than this one**: `_worker_findings` sees only
  `findings.append(<literal>)` on a bare name, and nothing cross-checks it. It feeds the single
  `_INDIRECT_SITES` exemption; see its own docstring for the scope.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
SRC: Final = REPO_ROOT / "src" / "fleet"
SCHEMA_PATH: Final = SRC / "state" / "schema.sql"
SPEC_PATH: Final = REPO_ROOT / "docs" / "SPEC.md"

#: The `findings.kind` comment block, in both copies, runs from the `kind` column declaration to
#: the next column. Anchored on the declaration itself rather than on prose, so re-wrapping the
#: commentary inside it cannot move the boundary.
_BLOCK_START: Final = re.compile(r"\n    kind       TEXT NOT NULL,")
_BLOCK_END: Final = re.compile(r"\n    severity   TEXT NOT NULL")
_EMITTED_HEADING: Final = "EMITTED BUT NEVER DECLARED"
_QUOTED: Final = re.compile(r"'([^']+)'")

#: `f"FileTooLarge:{rel}"` in the emitter and `'FileTooLarge:<path>'` in the listing name one kind.
#: Both sides collapse any `<...>` placeholder to `<>` before they are compared.
_PLACEHOLDER: Final = re.compile(r"<[^>]*>")

#: Sites whose `kind` is not resolvable from the statement itself, with the channel that supplies
#: it. Declaring a site here is the ONLY way to have an unresolved site not fail; a new one shows
#: up as a failure naming its module and SQL, which is the point.
#:
#: `cli.py`'s scan persist and its §3.1 gate both bind `kind` from a comprehension over worker
#: output (`output.findings`, `evidence.gated`), so the literals live in the workers.
_WORKER_FINDINGS: Final = "worker-findings"
_INDIRECT_SITES: Final = {
    ("cli.py", "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload, "
               "created_at) VALUES (?, ?, ?, 'warn', ?, ?, ?)"): _WORKER_FINDINGS,
}


# ----------------------------------------------------------------------------------------
# the listings
# ----------------------------------------------------------------------------------------


def _normalise(kind: str) -> str:
    return _PLACEHOLDER.sub("<>", kind)


def _listing(text: str, path: Path) -> tuple[frozenset[str], frozenset[str] | None]:
    """`(every name in the block, the names under the EMITTED heading or None if it is gone)`.

    A missing heading is reported rather than raised, so that a listing which lost it still gets
    its membership checked — an error at extraction time would mask the very drift being looked
    for. `test_every_name_under_the_emitted_heading_still_has_a_writer` is where its absence
    fails, because that is the only property the heading carries.
    """
    start = _BLOCK_START.search(text)
    assert start is not None, f"{path}: no `findings.kind` column declaration"
    end = _BLOCK_END.search(text, start.end())
    assert end is not None, f"{path}: `findings.kind` block has no terminating column"
    block = text[start.end() : end.start()]
    _, heading, emitted = block.partition(_EMITTED_HEADING)
    return (
        frozenset(_normalise(n) for n in _QUOTED.findall(block)),
        frozenset(_normalise(n) for n in _QUOTED.findall(emitted)) if heading else None,
    )


# ----------------------------------------------------------------------------------------
# the emitters
# ----------------------------------------------------------------------------------------

#: The findings table as a *statement* may name itself. `INSERT OR REPLACE`, a schema qualifier
#: and a quoted identifier are all the same write, and an unnormalised `"INSERT INTO findings" in
#: sql` test saw none of them — which is how a real site was made to vanish by reformatting alone.
_TABLE: Final = r"[\"'`\[]?(?:\w+\s*\.\s*)?findings\b"
_FINDINGS_INSERT: Final = re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+" + _TABLE, re.IGNORECASE)
_INSERT_RE: Final = re.compile(
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+" + _TABLE + r"[\"'`\]]?\s*"
    r"\(([^)]*)\)\s*VALUES\s*\(([^)]*)\)",
    re.IGNORECASE,
)

#: The same statement as it appears in *source text*, where a Python literal may interrupt it
#: anywhere: implicit concatenation, a line continuation, wrapping parentheses, an explicit `+`.
#: Deliberately looser than `_FINDINGS_INSERT` — it is the instrument that must not miss what the
#: AST walk misses, and `_recognition_gap` fails loudly if it ever becomes the narrower of the two.
_TEXT_INSERT: Final = re.compile(
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO[\s'\"()+\\]*(?:\w+\s*\.\s*)?findings\b", re.IGNORECASE
)
_FuncDef = (ast.FunctionDef, ast.AsyncFunctionDef)


class _Src:
    """Every module under `src/fleet`, parsed once, plus the lookups the resolver needs."""

    def __init__(self) -> None:
        self.trees: dict[Path, ast.Module] = {}
        for path in sorted(SRC.rglob("*.py")):
            self.trees[path] = ast.parse(path.read_text(encoding="utf-8"), str(path))
        self.constants: dict[str, set[str]] = {}
        #: Where each of those constants is written, so a site reached through one is attributed
        #: to the file and lines that hold its text rather than to the file that executes it.
        self.constant_defs: dict[str, list[tuple[Path, int, int]]] = {}
        self.calls: dict[str, list[ast.Call]] = {}
        for path, tree in self.trees.items():
            for node in tree.body:
                target, value = _assignment(node)
                constant = isinstance(value, ast.Constant) and isinstance(value.value, str)
                if target is not None and constant:
                    self.constants.setdefault(target, set()).add(value.value)  # type: ignore[union-attr]
                    self.constant_defs.setdefault(target, []).append((path, *_span(value)))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.calls.setdefault(node.func.id, []).append(node)

    def sql_of(
        self, expr: ast.expr, path: Path, scope: tuple[ast.AST, ...]
    ) -> tuple[str, Path, int, int] | None:
        """`(SQL text, and the file and line span of the literal it came from)`, or `None`.

        Three forms: an inline literal, a module-level single-valued constant, and a local the
        enclosing function assigns exactly once. The third is here because hoisting a long
        statement into a local to fit a line budget is an ordinary reformat, and a recognition
        step that drops a site on a reformat is the hole this module was defeated through. The
        span is what `_recognition_gap` matches its text-level hits against.
        """
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return expr.value, path, *_span(expr)
        if isinstance(expr, ast.Name):
            local = _bindings(expr.id, scope)
            if len(local) == 1:
                return self.sql_of(local[0], path, ())
            values = self.constants.get(expr.id, set())
            defs = self.constant_defs.get(expr.id, [])
            if len(values) == 1 and len(defs) == 1:
                return next(iter(values)), *defs[0]
        return None


def _span(node: ast.AST) -> tuple[int, int]:
    """`(first line, last line)` of a node's source text, inclusive."""
    return node.lineno, node.end_lineno or node.lineno  # type: ignore[attr-defined]


def _assignment(node: ast.stmt) -> tuple[str | None, ast.expr | None]:
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        if isinstance(target, ast.Name):
            return target.id, node.value
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id, node.value
    return None, None


def _scopes(tree: ast.Module) -> dict[int, tuple[ast.AST, ...]]:
    """`id(node) -> enclosing function chain, innermost last`. One pass, no parent pointers."""
    out: dict[int, tuple[ast.AST, ...]] = {}

    def walk(node: ast.AST, stack: tuple[ast.AST, ...]) -> None:
        out[id(node)] = stack
        inner = (*stack, node) if isinstance(node, _FuncDef) else stack
        for child in ast.iter_child_nodes(node):
            walk(child, inner)

    walk(tree, ())
    return out


def _kind_slot(sql: str) -> tuple[int, str] | None:
    """`(index among the `?` placeholders, "")` for a bound kind, or `(-1, literal)`."""
    match = _INSERT_RE.search(" ".join(sql.split()))
    if match is None:
        return None
    columns = [c.strip() for c in match.group(1).split(",")]
    values = [v.strip() for v in match.group(2).split(",")]
    if "kind" not in columns or len(columns) != len(values):
        return None
    slot = values[columns.index("kind")]
    if slot != "?":
        return -1, slot.strip("'")
    return values[: columns.index("kind")].count("?"), ""


def _params_elements(
    expr: ast.expr, src: _Src, scope: tuple[ast.AST, ...]
) -> list[ast.expr] | None:
    """The per-row expression tuple an `execute`/`executemany` params argument yields."""
    if isinstance(expr, (ast.Tuple, ast.List)):
        if all(isinstance(e, (ast.Tuple, ast.List)) for e in expr.elts) and expr.elts:
            return None  # a list of literal rows: not a shape any site uses
        return list(expr.elts)
    if isinstance(expr, (ast.ListComp, ast.GeneratorExp)):
        return _params_elements(expr.elt, src, scope)
    if isinstance(expr, ast.Name):
        bound = _bindings(expr.id, scope)
        if len(bound) == 1:
            return _params_elements(bound[0], src, scope)
    return None


def _bindings(name: str, scope: tuple[ast.AST, ...]) -> list[ast.expr]:
    """*Every* value assigned to `name` in the innermost enclosing function that assigns it.

    All of them, not the first one `ast.walk` reaches. A `kind` reassigned under an `if` is the
    ordinary alternative to the `if`-expression `_resolve` already handles, and returning one of
    its two literals is a silent under-report — the exact failure mode this module exists to
    remove. Callers that can union the results do; callers that cannot treat more than one
    binding as "not understood", which routes the site to the loud gate.
    """
    for func in reversed(scope):
        found = [
            value
            for node in ast.walk(func)
            for target, value in [_assignment(node)]
            if target == name and value is not None
        ]
        if found:
            return found
    return []


def _parameter_values(name: str, src: _Src, scope: tuple[ast.AST, ...]) -> set[str] | None:
    """`name` as a parameter of the innermost enclosing function: its default + every call site."""
    for func in reversed(scope):
        if not isinstance(func, _FuncDef):
            continue
        args = func.args
        positional = args.posonlyargs + args.args
        named = {a.arg for a in positional + args.kwonlyargs}
        if name not in named:
            continue
        out: set[str] = set()
        with_defaults = positional[len(positional) - len(args.defaults) :]
        defaults = dict(
            zip([a.arg for a in with_defaults], args.defaults, strict=True)
        )
        defaults.update(
            {
                a.arg: d
                for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True)
                if d is not None
            }
        )
        if name in defaults:
            resolved = _resolve(defaults[name], src, ())
            if resolved is None:
                return None
            out |= resolved
        for call in src.calls.get(func.name, []):
            keywords = {k.arg: k.value for k in call.keywords}
            if name not in keywords:
                if name in defaults:
                    continue
                return None  # passed positionally, or not at all: do not guess
            resolved = _resolve(keywords[name], src, ())
            if resolved is None:
                return None
            out |= resolved
        return out or None
    return None


def _resolve(expr: ast.expr, src: _Src, scope: tuple[ast.AST, ...]) -> set[str] | None:
    """Every string literal `expr` can evaluate to, or `None` when the form is not understood."""
    if isinstance(expr, ast.Constant):
        return {expr.value} if isinstance(expr.value, str) else None
    if isinstance(expr, ast.JoinedStr):
        out = ""
        for part in expr.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                out += part.value
            else:
                out += "<>"
        return {out}
    if isinstance(expr, ast.IfExp):
        left = _resolve(expr.body, src, scope)
        right = _resolve(expr.orelse, src, scope)
        return None if left is None or right is None else left | right
    if isinstance(expr, ast.Name):
        bound = _bindings(expr.id, scope)
        if bound:
            out: set[str] = set()
            for assigned in bound:
                resolved = _resolve(assigned, src, scope)
                if resolved is None:
                    return None
                out |= resolved
            return out
        from_parameter = _parameter_values(expr.id, src, scope)
        if from_parameter is not None:
            return from_parameter
        return src.constants.get(expr.id) or None
    return None


def _emitters() -> tuple[frozenset[str], tuple[str, ...]]:
    """`(every kind a findings writer can emit, the sites this module could not resolve)`."""
    src = _Src()
    kinds: set[str] = set()
    unresolved: list[str] = []
    #: `(file, first line, last line)` of every SQL literal the walk below recognised. This is
    #: what `_recognition_gap` cross-checks; without it, recognition is a silent pre-filter.
    recognised: set[tuple[Path, int, int]] = set()
    worker_channel_needed = False
    for path, tree in src.trees.items():
        scopes = _scopes(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in {"execute", "executemany"} or not node.args:
                continue
            found = src.sql_of(node.args[0], path, scopes[id(node)])
            if found is None:
                continue
            sql, sql_path, first, last = found
            if _FINDINGS_INSERT.search(" ".join(sql.split())) is None:
                continue
            recognised.add((sql_path, first, last))
            slot = _kind_slot(sql)
            site = (path.name, " ".join(sql.split()).split(" ON CONFLICT")[0].strip())
            if slot is None:
                unresolved.append(f"{path.name}: unparsable findings INSERT: {site[1]}")
                continue
            index, literal = slot
            if index < 0:
                kinds.add(_normalise(literal))
                continue
            elements = None
            if len(node.args) > 1:
                elements = _params_elements(node.args[1], src, scopes[id(node)])
            # A params tuple carries one element per `?`, in order — `NULL` and quoted columns in
            # the VALUES list occupy no slot — so `index`, which counts `?` alone, indexes it.
            resolved = None
            if elements is not None and index < len(elements):
                resolved = _resolve(elements[index], src, scopes[id(node)])
            if resolved is None:
                if _INDIRECT_SITES.get(site) == _WORKER_FINDINGS:
                    worker_channel_needed = True
                    continue
                unresolved.append(f"{path.name}: unresolved `kind` at {site[1]}")
                continue
            kinds |= {_normalise(k) for k in resolved}
    unresolved.extend(_recognition_gap(src, recognised))
    if worker_channel_needed:
        kinds |= _worker_findings(src)
    return frozenset(kinds), tuple(sorted(unresolved))


def _text_insert_sites(text: str, tree: ast.Module) -> list[tuple[int, str]]:
    """`(line, that line stripped)` for every findings INSERT in one module's *source text*.

    Prose is excluded, and only prose: a bare string statement (module, class, function and
    attribute docstrings alike — `orchestrator/stubs.py` describes a `cli.py` INSERT inside one)
    and anything from a `#` onwards. Neither can execute, so neither is a writer; everything else
    that spells the statement must be a site the AST walk named.
    """
    prose: set[int] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)):
            continue
        if not isinstance(node.value.value, str):
            continue
        first, last = _span(node)
        prose.update(range(first, last + 1))
    comment_at: dict[int, int] = {}
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT:
            row, col = token.start
            comment_at[row] = min(col, comment_at.get(row, col))
    lines = text.splitlines()
    out: list[tuple[int, str]] = []
    for match in _TEXT_INSERT.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        column = match.start() - (text.rfind("\n", 0, match.start()) + 1)
        if line in prose or column >= comment_at.get(line, len(text)):
            continue
        out.append((line, lines[line - 1].strip()))
    return out


def _recognition_gap(src: _Src, recognised: set[tuple[Path, int, int]]) -> list[str]:
    """The two instruments compared, in both directions. Disagreement is a failure, not a note.

    The AST walk decides what a findings writer *is*; the text scan only asks where the statement
    is spelled. They are derived differently and can only agree by both being right, so:

    * a text site no recognised literal covers is a writer the walk lost — the C-1 defect, where
      reformatting one real `cli.py` INSERT into a triple-quoted block took it out of the walk's
      view with every test still green;
    * a recognised literal no text site falls inside means the *cross-check* has gone blind, and
      a silently blind instrument is what this whole module exists to prevent. Failing on it is
      the calibration: neither instrument is allowed to become the narrower one unnoticed.
    """
    covered: dict[Path, set[int]] = {}
    for path, first, last in recognised:
        covered.setdefault(path, set()).update(range(first, last + 1))
    seen: dict[Path, set[int]] = {}
    out: list[str] = []
    for path, tree in src.trees.items():
        for line, snippet in _text_insert_sites(path.read_text(encoding="utf-8"), tree):
            seen.setdefault(path, set()).add(line)
            if line not in covered.get(path, set()):
                out.append(
                    f"{path.name}:{line}: a findings INSERT the AST walk never recognised, so its "
                    f"`kind` was never checked against the listings: {snippet}"
                )
    for path, first, last in recognised:
        if not seen.get(path, set()) & set(range(first, last + 1)):
            out.append(
                f"{path.name}:{first}: the AST walk recognised a findings INSERT that the text "
                f"cross-check cannot see — the two instruments have drifted apart"
            )
    return out


def _worker_findings(src: _Src) -> set[str]:
    """String literals appended to a worker's `findings` list — the kinds the scan persist writes.

    Scoped to `src/fleet/workers/`, where `findings` is documented as carrying KINDS only
    (`workers/clone.py:148`). The `GraphFinding`/`RewriteFinding` objects that other modules append
    to a list of the same name are not string literals and are not collected.
    """
    out: set[str] = set()
    for path, tree in src.trees.items():
        if path.parent.name != "workers":
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "append" or not isinstance(node.func.value, ast.Name):
                continue
            if node.func.value.id != "findings" or len(node.args) != 1:
                continue
            resolved = _resolve(node.args[0], src, ())
            if resolved:
                out |= {_normalise(k) for k in resolved}
    return out


# ----------------------------------------------------------------------------------------
# the tests
# ----------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def listings() -> dict[str, tuple[frozenset[str], frozenset[str] | None]]:
    return {
        "schema.sql": _listing(SCHEMA_PATH.read_text(encoding="utf-8"), SCHEMA_PATH),
        "SPEC.md": _listing(SPEC_PATH.read_text(encoding="utf-8"), SPEC_PATH),
    }


@pytest.fixture(scope="module")
def emitted() -> frozenset[str]:
    kinds, unresolved = _emitters()
    assert not unresolved, "\n".join(unresolved)
    return kinds


def test_the_two_findings_kind_listings_name_the_same_set(listings) -> None:
    """`schema.sql` and its `docs/SPEC.md` copy are one listing kept in two files.

    They have drifted apart before: at `8ea1881` the SPEC copy was two names and the whole CAVEAT
    behind `schema.sql`. Nothing but this assertion notices, because neither file is executed.
    """
    schema, spec = listings["schema.sql"][0], listings["SPEC.md"][0]
    assert schema - spec == frozenset(), "in schema.sql, missing from docs/SPEC.md"
    assert spec - schema == frozenset(), "in docs/SPEC.md, missing from schema.sql"


def test_every_kind_an_emitter_can_write_is_listed(listings, emitted) -> None:
    """The direction that failed: 19 kinds had live writers and appeared in no listing.

    A new `kind=` value, a new literal in a findings INSERT, or a new `findings.append(...)` in a
    worker fails here until both copies name it.
    """
    for name, (declared, _) in listings.items():
        assert emitted - declared == frozenset(), f"emitted by src/, unlisted in {name}"


def test_every_name_under_the_emitted_heading_still_has_a_writer(listings, emitted) -> None:
    """The other direction, bound only where the listing actually claims emission.

    The `Shipped:`/DECLARED half is exempt by its own CAVEAT — `BaselineRed`, `PreflightFailed`,
    `RuleConflict` are read by Python that nothing writes, and five names have no Python at all.
    That is a stated property of that half. The EMITTED heading makes the stronger claim ("each of
    these has a live writer in src/"), so deleting the last writer of one of its names fails here.
    """
    for name, (_, emitted_section) in listings.items():
        assert emitted_section is not None, f"{name} lost its {_EMITTED_HEADING!r} heading"
        assert emitted_section - emitted == frozenset(), f"listed as emitted in {name}, no writer"


def test_every_findings_writer_in_src_is_one_this_module_resolved() -> None:
    """The gate that stops this file becoming another snapshot.

    A detector that silently stops seeing emitters reports "no drift" forever — which is exactly
    how a listing accumulated a 19-kind gap while a review reported one. So an `INSERT INTO
    findings` whose `kind` cannot be resolved to string literals is a failure naming the site,
    and the only way to exempt one is to declare its channel in `_INDIRECT_SITES`.

    That gate only ever saw the sites recognition handed it, and recognition was an unnormalised
    substring test: reformatting one real `cli.py` INSERT into a triple-quoted block removed it
    from the count with all four tests still passing. So this asserts the *recognition* step too,
    against a text-level derivation of the same sites (`_recognition_gap`) that no reformat of a
    Python string literal can move.
    """
    _, unresolved = _emitters()
    assert unresolved == (), "\n".join(unresolved)
