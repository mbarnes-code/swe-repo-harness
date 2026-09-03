"""Every method a test subclass defines must still be reachable — a rename must not disarm it.

WHY this file exists
--------------------
`tests/test_budgets.py` once measured a concurrency property by subclassing the limiter and
overriding a private method by name. A later refactor renamed that method as part of a clean,
well-reviewed change. Nothing bound the two together, so the override stopped being called: the
fuzz went on reporting green over a rewritten class, and the suite could not tell that result from
a real one. It was found by luck, not by a gate.

That failure has one property worth generalising: **the test named something the production code
was free to stop naming, and nothing checked the two still agreed.** Most ways a test can name a
production symbol are self-defending and are deliberately NOT checked here:

* `monkeypatch.setattr(target, "_name", value)` raises `AttributeError` when `_name` is gone —
  pytest checks it for us, unless the call passes `raising=False` (no `setattr` call in this suite
  does; the assertion below keeps it that way);
* reading an attribute (`original = forge._write_request`) raises on a rename;
* asserting a string appears (`assert "_drain" in charge_sites`) fails when it no longer does.

A subclass override is the shape with no such backstop. Defining `_wake_next` on a subclass of a
class that no longer has `_wake_next` is legal Python, costs nothing at import, and produces a
method nobody calls. So this file asserts the one property that would have caught it:

    every method defined on a test-local subclass overrides a name its base really has.

"Defined on" is not obvious and was, for one round, narrower than this sentence: it meant "a `def`
directly in the class body", and a review defeated the file with two ordinary rewrites that satisfy
the sentence while the body never saw them. `_defined_methods` is the answer to what it means now —
three shapes, each measured, each with its own reproduced defeat.

A test fake may legitimately carry a method its base does not have — a recorder's `reset()`
classmethod, say. Those are named in `NOT_OVERRIDES` with a reason, and the allowlist is a
ratchet in both directions: `test_the_allowlist_has_not_gone_stale` fails the day one of those
names *does* appear on its base, so an entry cannot outlive the reason it was written.

An earlier draft accepted "the method is called by name somewhere in the file" instead of the
allowlist. It was rejected on measurement, not on taste: under a synthetic rename of
`EcosystemAdapter.import_specifier` the check stayed green, because a same-named call on an
unrelated object elsewhere in the file vouched for the orphan. The allowlist has no such hole.

What this CANNOT see
--------------------
It checks that a *name* still exists on the base, not that production still *calls* it. A refactor
that keeps `_drain` defined but stops calling it disarms an override just as completely and passes
here — as does a base that grows an unrelated method colliding with a fake's helper name. It sees
overrides only: an instrument that hooks by any other means is outside it. It is one shape, gated;
the honest scope is written down here rather than implied by the file's name.

**Where "defined on the class" stops.** The walk reads a class *body*, statically. Every binding
below was tried against this file after the three shapes above were bound, and every one of them
is green while the override is dead — measured, not supposed:

* `Fake.method = _f` **after** the class statement, and any `setattr(Fake, ...)` or `type(...)`
  construction. The body is where this looks; nothing outside it is a class-body statement.
* a class-body assignment whose value is not syntactically a function: `functools.partial(_f)`,
  an attribute (`_f.__get__`), a factory call. Only a `lambda`, a name this module binds with
  `def`/`lambda`, and those wrapped in `staticmethod`/`classmethod` are read as methods — the
  narrow rule exists because counting every class-body assignment reports 49 of this suite's 64 as
  orphans, and a detector that fires on correct code is worse than the gap it closes.
* a `def` under a `match`/`case` in a class body. `_COMPOUND` covers `if`/`try`/`with`/`for`/
  `while`, the shapes a version or import guard actually uses; `match` was left out deliberately
  rather than missed, and is recorded here instead. Nobody writes it; if someone does, this file
  goes quiet about that method and says so here first.

**And where the `setattr` gate stops.** It now refuses the builtin `setattr(...)` outright (it
never raises, so it is exactly the silent kind this module excludes) and accepts only a literal
`raising=True`, because the previous `== "False"` string test passed `raising=bool(0)` and
`raising=RAISING`. Both are inverted enumerations, per CLAUDE.md Rule 12. What is still invisible:
`monkeypatch.setattr("module.attr", value)` in the *string* form, and any patch reached through an
alias this file's `ast.unparse` does not spell as ending in `setattr`.

**Builtin bases.** An unresolvable base is a hard failure, not a skip -- a base this cannot see
is a class it is not checking, and saying so loudly is the right default. But a *builtin* base is
not unseeable. `class _RecordingEvidence(dict)`, landed at `42a4369`, made this file **red on
`main`**: `dict` is in no import table, so the class went to the unresolved list and its methods
were never examined. Name resolution now ends where Python's does -- imports, then module-local
classes, then `builtins` -- with `local` deliberately ahead of `builtins` so a module-local class
named `dict` still shadows the builtin here as it does at runtime. Class result: unresolved bases
**1 -> 0**, methods examined **34 -> 35** (measured at `1ce1901`). Not a widening: a method the
builtin does not carry is still an orphan, and on a synthetic `_RecordingEvidence.kyes` this file
now names it, where the previous version could only say it could not resolve `dict`.

**Duplicate class names.** Two classes of one name in one file used to mean the last one `ast.walk`
reached answered for both — so a sibling fake's method set could vouch for a real orphan, and one
`NOT_OVERRIDES` line could exempt two different classes. Both are now loud rather than latent, and
the shape is not hypothetical: `test_budgets.py:696`/`:1272` and
`test_llm_backend_bedrock.py:400`/`:513` already carry one each. They stay silent today only
because neither is used as a base and neither is exempted; the guard is what keeps that from being
load-bearing.
"""

from __future__ import annotations

import ast
import builtins
import importlib
from collections.abc import Iterator
from pathlib import Path

TESTS = Path(__file__).resolve().parent

# `file:Class.method` -> why this method is allowed to override nothing. Every entry is a claim
# that the name is a fake's own helper, not an instrument whose base counterpart went away.
NOT_OVERRIDES: dict[str, str] = {
    "test_workers_base.py:UnitWorker.reset": (
        "a classmethod that clears the fake's ClassVar recorders between tests; BaseWorker has "
        "no reset and is not expected to grow one"
    ),
    "test_d89_phase2_claim_lifecycle.py:Visitor.visit_Call": (
        "an ast.NodeVisitor dispatch hook, not an override in this checker's sense: "
        "NodeVisitor.visit() finds visit_Call via getattr('visit_' + node type) at runtime, so "
        "the base class never literally defines visit_Call for this checker's static resolver "
        "to find — a rename here disarms the AST walk silently, but not by this mechanism"
    ),
    "test_repository_no_list_returns.py:_Visitor.visit_FunctionDef": (
        "an ast.NodeVisitor dispatch hook, resolved by NodeVisitor.visit() via "
        "getattr('visit_' + node type) at runtime rather than declared on the base class — same "
        "shape as the Visitor.visit_Call entry above, so a rename here disarms the walk silently "
        "without tripping this checker's static resolver"
    ),
    "test_repository_no_list_returns.py:_Visitor.visit_AsyncFunctionDef": (
        "an ast.NodeVisitor dispatch hook, resolved by NodeVisitor.visit() via "
        "getattr('visit_' + node type) at runtime rather than declared on the base class — same "
        "shape as the Visitor.visit_Call entry above, so a rename here disarms the walk silently "
        "without tripping this checker's static resolver"
    ),
    "test_repository_no_list_returns.py:_Visitor._check": (
        "a fake's own helper, not an instrument whose base counterpart went away: the visitor's "
        "private dispatch routine, invoked directly by its two visit_* methods rather than by "
        "ast.NodeVisitor machinery, so ast.NodeVisitor is not expected to carry it"
    ),
}


_DEF = (ast.FunctionDef, ast.AsyncFunctionDef)
#: Class-body statements that can hide a `def` from a shallow `node.body` scan. A method defined
#: under one of these is a method: Python executes the class body, so the name lands on the class
#: exactly as a top-level `def` would, and a rename of its base counterpart disarms it identically.
_COMPOUND = (ast.If, ast.Try, ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While)


def _local_classes(tree: ast.Module) -> dict[str, ast.ClassDef | None]:
    """Name -> the class of that name in this module, or `None` when there is more than one.

    `{n.name: n for n in ast.walk(tree)}` kept whichever definition `ast.walk` reached last, and
    two duplicate names already exist in this suite (`test_budgets.py`'s two `_NullExecutor`s at
    `:696`/`:1272`, `test_llm_backend_bedrock.py`'s two `NoEffortTarget`s at `:400`/`:513`), latent
    only because neither is currently used as a base. The moment one is, a *sibling* fake's method
    set vouches for the other's orphan and this file goes green over a disarmed instrument. Marking
    the name ambiguous routes it to the `unresolved` list, which is a hard failure, not a skip.
    """
    out: dict[str, ast.ClassDef | None] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            out[node.name] = None if node.name in out else node
    return out


def _function_names(tree: ast.Module) -> set[str]:
    """Every name this module binds to a function: a `def`, or a name assigned a `lambda`."""
    names = {node.name for node in ast.walk(tree) if isinstance(node, _DEF)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Lambda):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return names


def _defined_methods(node: ast.ClassDef, functions: set[str]) -> list[str]:
    """Every method name a class body binds, in any of the three shapes a class body can use.

    A shallow `for s in node.body if isinstance(s, FunctionDef)` saw only the first, and the other
    two are green-while-false shapes that were reproduced before this was widened:

    * **`def` under an `if`/`try`** — `if sys.version_info >= (3, 12):` around an override, an
      ordinary non-adversarial edit. Measured: wrapping `UnitWorker.on_cancel` that way and then
      renaming `BaseWorker.on_cancel` away left all three cases green, where the unwrapped form
      fails. Nothing in the suite uses this shape today (measured: 0), so binding it costs nothing.
    * **assignment-bound override** — `on_cancel = _scripted_on_cancel` in the class body. Same
      measurement, same silence. Only a value that is a *function* counts: a bare name bound by a
      `def` or `lambda` in the same module, a literal `lambda`, or one of those wrapped in
      `staticmethod`/`classmethod`. Counting every class-body assignment instead would report 49
      of this suite's 64 as orphans — `input_model = Units`, `phase = PHASE` and the rest of the
      ClassVar register — which is a detector firing on correct code, worse than the gap
      (CLAUDE.md Rule 12's stop rule). The narrow rule reports **0** today and catches the defeat.
    """
    names: list[str] = []

    def collect(statements: list[ast.stmt]) -> None:
        for statement in statements:
            if isinstance(statement, _DEF):
                names.append(statement.name)
            elif isinstance(statement, _COMPOUND):
                collect(statement.body)
                collect(getattr(statement, "orelse", []))
                collect(getattr(statement, "finalbody", []))
                for handler in getattr(statement, "handlers", []):
                    collect(handler.body)
            else:
                names.extend(_assigned_method(statement, functions))

    collect(node.body)
    return [name for name in names if not (name.startswith("__") and name.endswith("__"))]


def _assigned_method(statement: ast.stmt, functions: set[str]) -> list[str]:
    """Targets of a class-body assignment whose value is a function. See `_defined_methods`."""
    if isinstance(statement, ast.Assign):
        targets = [t.id for t in statement.targets if isinstance(t, ast.Name)]
    elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
        targets = [statement.target.id]
    else:
        return []
    value = statement.value
    if (
        isinstance(value, ast.Call)
        and ast.unparse(value.func) in {"staticmethod", "classmethod"}
        and value.args
    ):
        value = value.args[0]
    if isinstance(value, ast.Lambda):
        return targets
    if isinstance(value, ast.Name) and value.id in functions:
        return targets
    return []


def _test_sources() -> Iterator[tuple[Path, ast.Module]]:
    """Every module of this suite. `fixtures/` is excluded: those files are inputs the suite
    feeds to the harness — sample repos, some of them deliberately malformed — not instruments,
    and parsing them here would turn a fixture's intended syntax error into this file's failure.
    """
    for path in sorted(TESTS.rglob("*.py")):
        if "__pycache__" in path.parts or "fixtures" in path.relative_to(TESTS).parts:
            continue
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def _import_table(tree: ast.Module) -> dict[str, tuple[str, str | None]]:
    """Name -> (module, attribute). Walks the whole tree: this suite imports inside test bodies."""
    table: dict[str, tuple[str, str | None]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                table[alias.asname or alias.name] = (node.module, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                table[alias.asname or alias.name.split(".")[0]] = (alias.name, None)
    return table


def _unsubscript(node: ast.expr) -> ast.expr:
    """`BaseWorker[Units, Landed]` names the same class as `BaseWorker`."""
    return node.value if isinstance(node, ast.Subscript) else node


def _resolve(expr: str, table: dict[str, tuple[str, str | None]]) -> object | None:
    root, *rest = expr.split(".")
    if root not in table:
        return None
    module_name, attribute = table[root]
    try:
        obj: object = importlib.import_module(module_name)
        if attribute is not None:
            obj = getattr(obj, attribute)
        for part in rest:
            obj = getattr(obj, part)
    except (ImportError, AttributeError):
        return None
    return obj


def _attribute_names(
    expr: str,
    table: dict[str, tuple[str, str | None]],
    local: dict[str, ast.ClassDef | None],
    depth: int = 0,
) -> set[str] | None:
    """Names carried by the base `expr`, or None if the base could not be resolved at all."""
    if depth > 8:  # a cycle in locally-defined bases; treat as unresolvable rather than hang
        return None
    obj = _resolve(expr, table)
    if obj is not None:
        names = set(dir(obj))
        names |= set(getattr(obj, "model_fields", {}) or {})  # pydantic fields are not in dir()
        return names
    if expr in local:
        node = local[expr]
        if node is None:
            return None  # more than one class of this name here; see `_local_classes`
        names = set()
        for statement in node.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(statement.name)
            elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                names.add(statement.target.id)
            elif isinstance(statement, ast.Assign):
                names.update(t.id for t in statement.targets if isinstance(t, ast.Name))
        for base in node.bases:
            inherited = _attribute_names(ast.unparse(_unsubscript(base)), table, local, depth + 1)
            if inherited is None:
                return None
            names |= inherited
        return names
    # Last: builtins, which is where Python's own name lookup ends too, and deliberately AFTER
    # `local` so a module-local class named `dict` still shadows the builtin here as it does at
    # runtime. Without this a subclass of a builtin -- `class _RecordingEvidence(dict)`, landed at
    # `42a4369` -- made the base unresolvable, and an unresolvable base is a hard failure by
    # design. That is the right default (a base this cannot see is a class it is not checking),
    # but a builtin base is not unseeable: it is fully introspectable, so refusing it reported a
    # gap that did not exist and left the class genuinely unchecked. Resolving it is completing
    # the lookup path, not widening an exemption -- a method the builtin does not carry is still
    # an orphan, and is now reported as one instead of being lost in the unresolved list.
    builtin = getattr(builtins, expr, None)
    if isinstance(builtin, type):
        return set(dir(builtin))
    return None


def test_no_test_subclass_defines_a_method_its_base_no_longer_has() -> None:
    orphans: list[str] = []
    unresolved: list[str] = []
    checked = 0

    for path, tree in _test_sources():
        table = _import_table(tree)
        local = _local_classes(tree)
        functions = _function_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or not node.bases:
                continue
            methods = _defined_methods(node, functions)
            if not methods:
                continue
            inherited: set[str] = set()
            for base in node.bases:
                expr = ast.unparse(_unsubscript(base))
                names = _attribute_names(expr, table, local)
                if names is None:
                    unresolved.append(f"{path.name}:{node.lineno} {node.name} -> {expr}")
                else:
                    inherited |= names
            for method in methods:
                checked += 1
                key = f"{path.name}:{node.name}.{method}"
                if method not in inherited and key not in NOT_OVERRIDES:
                    orphans.append(
                        f"{path.name}:{node.lineno} {node.name}.{method} overrides nothing on "
                        f"{', '.join(ast.unparse(_unsubscript(b)) for b in node.bases)} — either a "
                        f"rename disarmed it, or it is a fake's own helper and belongs in "
                        f"NOT_OVERRIDES with a reason"
                    )

    assert not unresolved, (
        "this check cannot resolve these bases, so it is not checking those classes; make the base "
        "importable by name rather than deleting the assertion:\n  " + "\n  ".join(unresolved)
    )
    assert not orphans, "\n  ".join(["disarmed or dead test methods:", *orphans])
    assert checked >= 30, (
        f"only {checked} subclass methods were examined; the walk stopped finding them, which "
        "would make a pass here meaningless"
    )


def test_no_setattr_in_the_suite_opts_out_of_pytest_s_existence_check() -> None:
    """`monkeypatch.setattr` is this suite's other name-binding instrument, and it is safe only
    because it raises when the attribute is gone. `raising=False` turns it into the silent kind.
    """
    offenders: list[str] = []
    for path, tree in _test_sources():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = ast.unparse(node.func)
            if func == "setattr":
                offenders.append(
                    f"{path.name}:{node.lineno} the builtin setattr(...), which never raises"
                )
                continue
            if not func.endswith(("setattr", "patch.object")):
                continue
            for keyword in node.keywords:
                if keyword.arg == "raising" and ast.unparse(keyword.value) != "True":
                    offenders.append(
                        f"{path.name}:{node.lineno} {func}(..., raising="
                        f"{ast.unparse(keyword.value)})"
                    )

    assert not offenders, (
        "these patches bind a name without checking it exists, so a rename disarms them "
        "silently:\n  " + "\n  ".join(offenders)
    )


def test_the_allowlist_has_not_gone_stale() -> None:
    """An allowlist that outlives its reason quietly re-authorises the next defect.

    Every `NOT_OVERRIDES` entry claims the base does NOT carry that name. The day a base grows
    it, the entry stops being an exemption and starts being a blindfold over a real override, so
    this fails until the line is deleted.
    """
    still_absent: list[str] = []
    for path, tree in _test_sources():
        table = _import_table(tree)
        local = _local_classes(tree)
        functions = _function_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or not node.bases:
                continue
            for method in _defined_methods(node, functions):
                key = f"{path.name}:{node.name}.{method}"
                if key not in NOT_OVERRIDES:
                    continue
                for base in node.bases:
                    names = _attribute_names(ast.unparse(_unsubscript(base)), table, local)
                    if names and method in names:
                        still_absent.append(
                            f"{key} is now a real override of "
                            f"{ast.unparse(_unsubscript(base))}; delete its NOT_OVERRIDES line"
                        )

    assert not still_absent, "\n  ".join(["stale exemptions:", *still_absent])

    seen = {
        f"{path.name}:{node.name}.{method}"
        for path, tree in _test_sources()
        for functions in [_function_names(tree)]
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.bases
        for method in _defined_methods(node, functions)
    }
    assert not (set(NOT_OVERRIDES) - seen), (
        "these exemptions name methods that no longer exist; delete them: "
        f"{sorted(set(NOT_OVERRIDES) - seen)}"
    )

    # An exemption is keyed `file:Class.method`, so if a file defines that class name twice the
    # one line exempts BOTH of them — the second silently inheriting a reason written about the
    # first. Loud rather than latent: two duplicate class names already exist in this suite.
    ambiguous = [
        key
        for path, tree in _test_sources()
        for name, node in _local_classes(tree).items()
        if node is None
        for key in NOT_OVERRIDES
        if key.startswith(f"{path.name}:{name}.")
    ]
    assert not ambiguous, (
        "these exemptions name a class this file defines more than once, so one reason exempts "
        f"every class of that name: {sorted(ambiguous)}. Rename one of the classes."
    )
