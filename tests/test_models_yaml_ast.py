"""SPEC §12 item 40's own literal text (`docs/SPEC.md:7455`): "...and an AST test asserts that
the only module in `src/fleet/` reading `config/models.yaml` is `settings.py`." No such test
existed (`docs/CRITERIA_PLAN.md` §40's "Done bar": "write the one AST test the M1 note names.
Everything else in this criterion is already closed.") — only the model-id/endpoint greps were
covered.

This is that AST test. Modeled on `tests/test_ddl_ast.py`'s convention (a docstring-excluding
`ast.walk` scan, a positive control proving the extractor can find something, then the real
negative assertion) rather than a text grep, because a text grep over "models.yaml" would also
match every PROSE mention of the file — of which `src/fleet/` has many (docstrings, error
messages, `llm/roles.py`'s own documentation that it is handed an ALREADY-PARSED
`config/models.yaml` tree rather than opening the file itself, per round-K's finding quoted in
`docs/SPEC.md`'s §12.40 dated annotation).

**What "reads" means here, made mechanical.** `settings.py:1138` is the one place the path is
actually constructed for an open: `models_path = cfg_dir / "models.yaml"`, immediately passed to
`_read_yaml_mapping(models_path)` at `:1149`. That literal — the bare filename `"models.yaml"`,
with no surrounding sentence — is the shape a real path-join uses, and it is measurably different
from a prose mention: every other occurrence of the string "models.yaml" under `src/fleet/`
carries surrounding words in the SAME literal (an f-string segment like
`"in config/models.yaml; have "` or a docstring sentence), because a person writing prose about
the file does not also happen to write nothing else in that literal. So the extractor below
matches a `Constant` string node, stripped, against EXACTLY `models.yaml` or `.../models.yaml`
(covers both the bare join and settings.py's own `"config/models.yaml"` labels) — anchored at
both ends, not a substring search — and additionally excludes docstring literals by tree
position (identical convention to `test_ddl_ast.py::_docstring_node_ids`), so prose describing
the file cannot trip the scan even in the one-in-a-million case it has no other words in the
same literal.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src" / "fleet"
_SETTINGS_REL = "src/fleet/settings.py"

#: Anchored at both ends against a Constant string's value AFTER `.strip()`: either the bare
#: filename (`cfg_dir / "models.yaml"`) or the filename with its `config/` directory prefix
#: (`file="config/models.yaml"`). Deliberately NOT a substring search — see the module docstring
#: for why a substring search over "models.yaml" would false-positive on every prose mention.
_MODELS_YAML_PATH = re.compile(r"^(?:.*/)?models\.yaml$")


def _docstring_node_ids(tree: ast.Module) -> frozenset[int]:
    """Same convention as `tests/test_ddl_ast.py::_docstring_node_ids`: exclude a module's,
    class's or function's own docstring literal by tree POSITION, not by content, so prose
    *describing* `config/models.yaml` cannot trip the scan merely by matching the pattern."""
    docs: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docs.add(id(first.value))
    return frozenset(docs)


def _py_files() -> list[Path]:
    return sorted(_SRC.rglob("*.py"))


def _models_yaml_sites() -> list[tuple[str, int, str]]:
    """`(relpath, line, value)` for every non-docstring string literal under `src/fleet/` whose
    value, stripped, is exactly `models.yaml` or ends `/models.yaml` with nothing else in the
    same literal."""
    found: list[tuple[str, int, str]] = []
    for path in _py_files():
        rel = path.relative_to(_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        docs = _docstring_node_ids(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docs:
                continue
            value = node.value.strip()
            if _MODELS_YAML_PATH.match(value):
                found.append((rel, node.lineno, value))
    return sorted(found)


def test_the_models_yaml_extractor_finds_the_known_site_in_settings() -> None:
    """Positive control, read before any verdict below (CLAUDE.md Guardrail 6): `settings.py`
    genuinely constructs the `config/models.yaml` open path (`cfg_dir / "models.yaml"` inside
    `FleetSettings.load`, `:1138`), so a scan of the whole tree must find at least one hit there.
    If this reports zero, the extractor — not the tree — is broken, and
    `test_only_settings_reads_models_yaml` below would be passing vacuously rather than because
    the tree is actually clean.
    """
    found = _models_yaml_sites()
    assert found, (
        "the models.yaml-path extractor found zero anchored path literals anywhere under "
        "src/fleet/, including settings.py — this means the extractor itself is broken, not "
        "that settings.py stopped reading config/models.yaml"
    )
    assert all(rel == _SETTINGS_REL for rel, _line, _value in found), (
        f"the positive control itself found a hit outside {_SETTINGS_REL}: {found!r}"
    )


def test_only_settings_reads_models_yaml() -> None:
    """SPEC §12 item 40 (`docs/SPEC.md:7455`): "...an AST test asserts that the only module in
    `src/fleet/` reading `config/models.yaml` is `settings.py`." `llm/roles.py` is documented and
    built over an ALREADY-PARSED `config/models.yaml` tree (round-K's finding, quoted in
    `docs/SPEC.md`'s §12.40 dated annotation) — it never opens the file itself, and every other
    module under `src/fleet/` only ever mentions the file in prose. This test is what pins that
    the file-opening act belongs to `settings.py` alone.
    """
    found = _models_yaml_sites()
    offenders = [(rel, line, value) for rel, line, value in found if rel != _SETTINGS_REL]
    assert offenders == [], (
        "a config/models.yaml path literal was found outside src/fleet/settings.py: "
        + "; ".join(f"{rel}:{line}: {value!r}" for rel, line, value in offenders)
    )
