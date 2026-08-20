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
"""

from __future__ import annotations

import ast
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
}


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
    local: dict[str, ast.ClassDef],
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
    return None


def test_no_test_subclass_defines_a_method_its_base_no_longer_has() -> None:
    orphans: list[str] = []
    unresolved: list[str] = []
    checked = 0

    for path, tree in _test_sources():
        table = _import_table(tree)
        local = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or not node.bases:
                continue
            methods = [
                s.name
                for s in node.body
                if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not (s.name.startswith("__") and s.name.endswith("__"))
            ]
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
            if not func.endswith(("setattr", "patch.object")):
                continue
            for keyword in node.keywords:
                if keyword.arg == "raising" and ast.unparse(keyword.value) == "False":
                    offenders.append(f"{path.name}:{node.lineno} {func}(..., raising=False)")

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
        local = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or not node.bases:
                continue
            for statement in node.body:
                if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                key = f"{path.name}:{node.name}.{statement.name}"
                if key not in NOT_OVERRIDES:
                    continue
                for base in node.bases:
                    names = _attribute_names(ast.unparse(_unsubscript(base)), table, local)
                    if names and statement.name in names:
                        still_absent.append(
                            f"{key} is now a real override of "
                            f"{ast.unparse(_unsubscript(base))}; delete its NOT_OVERRIDES line"
                        )

    assert not still_absent, "\n  ".join(["stale exemptions:", *still_absent])

    seen = {
        f"{path.name}:{node.name}.{statement.name}"
        for path, tree in _test_sources()
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.bases
        for statement in node.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert not (set(NOT_OVERRIDES) - seen), (
        "these exemptions name methods that no longer exist; delete them: "
        f"{sorted(set(NOT_OVERRIDES) - seen)}"
    )
