"""`RewriteRule`, the `Rewriter` protocol, and the engine registry (SPEC §7.4, ADR-0006).

A rewrite is **data, not code**: adding a language's import-rewrite is a YAML file under
`config/rules/`, never an orchestrator edit. `extra="forbid"` on the model is what turns a
typo in that YAML into a startup `ValidationError` instead of a rule that silently never fires.

Two consequences of "data, not code" are load-bearing here:

* **`engine` is an open `str`**, resolved through the registry below — not a `Literal`. Closing
  the engine set in core would make the claim false for the fourth engine. An engine a rule
  names and the registry cannot resolve is a **startup** error (`UnresolvedReferenceError`,
  exit 2), exactly as an unknown `backend` is (§7.4, §7.7, §13 row 36); resolving lazily turns a
  typo into a wave-7 `KeyError` after hours of spend.
* **`Rewriter.apply` is pure `source` → patch.** It takes the text, not a path to re-read. An
  implementation that re-reads `path` from the worktree breaks composition, because the second
  rule would diff against a tree the first rule has not committed to — and the two diffs, both
  computed against the same original, would then make `git apply` reject the second.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Final, Protocol, runtime_checkable

import yaml  # type: ignore[import-untyped]  # types-PyYAML is not a dependency
from pydantic import Field, model_validator

from fleet.models.base import FleetModel
from fleet.models.tasks import FilePatch
from fleet.settings import ConfigFileError, ConfigValidationError, UnresolvedReferenceError

__all__ = [
    "EngineRegistry",
    "EngineUnavailableError",
    "RewriteRule",
    "Rewriter",
    "language_for_path",
    "load_rules",
    "render_template",
    "rule_matches_path",
    "rule_sort_key",
]

REWRITER_ATTR: Final = "REWRITER"
"""Every driver module exposes one ready-made `Rewriter` under this name, so `transform.engines`
can stay a plain name → module map in YAML with no factory protocol to specify."""

_PLACEHOLDER: Final = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

#: Extension → ast-grep language id. Small and explicit: an unknown extension makes every
#: language-scoped rule skip the file, which is the safe direction — a rule that does not run
#: leaves a file unmigrated and visible, while a rule that runs under the wrong grammar corrupts
#: it silently.
_LANGUAGE_BY_SUFFIX: Final[Mapping[str, str]] = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "jsx",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".mjs": "javascript",
    ".php": "php",
    ".py": "python",
    ".pyi": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".scala": "scala",
    ".swift": "swift",
    ".ts": "typescript",
    ".tsx": "tsx",
}


class EngineUnavailableError(RuntimeError):
    """A driver's underlying tool is not installed on this host (Rule 11).

    Deliberately an exception and never a `None` return: `None` means "this rule matched nothing",
    and a missing tool reported that way is indistinguishable from a clean no-op — which would
    ship an unrewritten file as a success.
    """


class RewriteRule(FleetModel):
    """Declarative rewrite, loaded from config/rules/*.yml. Data, not code — so adding a
    language's import-rewrite is a YAML file, never an orchestrator edit."""

    id: str = Field(min_length=1)
    description: str = ""
    engine: str = "ast-grep"
    """Resolved through the `Rewriter` registry, NOT a `Literal`: closing the engine set in core
    would make "data, not code" false for the fourth engine. Unresolvable is a startup error,
    exactly as an unknown backend is."""
    languages: list[str] = Field(  # ast-grep language ids: java, tsx, python, go, rust...
        min_length=1
    )
    applies_to: list[str] = Field(default_factory=lambda: ["**/*"])  # repo-relative globs
    rule: dict[str, Any] | None = None  # inline ast-grep rule
    rule_file: str | None = None  # or a path under config/rules/
    fix: str | None = None  # ast-grep rewrite template
    params: dict[str, str] = Field(default_factory=dict)  # {{old_pkg}}/{{new_pkg}} substitutions
    priority: int = 100

    @model_validator(mode="after")
    def _exactly_one_rule_source(self) -> RewriteRule:
        if (self.rule is None) == (self.rule_file is None):
            raise ValueError(
                f"rule {self.id!r}: exactly one of `rule` (inline) or `rule_file` is required"
            )
        return self


@runtime_checkable
class Rewriter(Protocol):
    """Implemented by rewrite/astgrep.py (primary), rewrite/libcst_py.py and
    rewrite/tsmorph.py (fenced secondaries, ADR-0006)."""

    engine: str

    async def apply(
        self, rule: RewriteRule, path: str, source: str, params: dict[str, str]
    ) -> FilePatch | None:
        """Pure `source` → patch. `path` is identity only — glob matching and language id — and
        `source` is the ONLY text read: an implementation that re-reads `path` from the worktree
        breaks composition, because the second rule would then diff against a tree the first rule
        has not committed to."""
        ...

    async def parse_probe(self, path: str) -> bool: ...


def rule_sort_key(rule: RewriteRule) -> tuple[int, str]:
    """`(priority, id)` — the total order §7.4 rule 1 requires.

    `id` breaks the tie so equal-priority rules cannot dispatch in YAML-load (i.e. filesystem)
    order, for the same reason §7.3 sorts workers by `(priority, name)`. Without it, two rules at
    priority 100 compose in whichever order `rglob` happened to yield, and the same fleet produces
    two different patches on two hosts.
    """
    return (rule.priority, rule.id)


def render_template(text: str, params: Mapping[str, str]) -> str:
    """Substitute `{{name}}` from `params`. An unknown placeholder raises rather than surviving
    into a rewrite: `import {{new_pkg}}.Foo` written literally into a source file is a compile
    error discovered a phase later, with no rule id attached to it."""
    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in params:
            raise KeyError(f"template references {{{{{name}}}}}, which no param supplies")
        return params[name]

    return _PLACEHOLDER.sub(_sub, text)


def language_for_path(path: str) -> str | None:
    """The ast-grep language id for a repo-relative path, or `None` if the suffix is unknown."""
    return _LANGUAGE_BY_SUFFIX.get(PurePosixPath(path).suffix.lower())


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a repo-relative glob to a regex. `**/` spans directories (and matches zero of
    them, so `**/*.py` covers a top-level `x.py`); `*` and `?` never cross a `/`.

    Hand-rolled rather than `fnmatch`, whose `*` happily crosses `/` — under `fnmatch`, an
    `applies_to: ["*.py"]` rule would fire on `vendor/generated/x.py` too.
    """
    out: list[str] = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out) + r"\Z")


def rule_matches_path(rule: RewriteRule, path: str) -> bool:
    """Does this rule claim this file? Language first, then any one `applies_to` glob."""
    language = language_for_path(path)
    if language is None or language not in rule.languages:
        return False
    return any(_glob_to_regex(pattern).match(path) is not None for pattern in rule.applies_to)


def _rules_in_file(path: Path) -> list[Mapping[str, Any]]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigFileError(f"is not a readable rule file: {exc}", file=path) from exc
    if loaded is None:
        return []
    body = loaded.get("rules") if isinstance(loaded, Mapping) else loaded
    if not isinstance(body, list):
        raise ConfigFileError("must be a list of rules, or a mapping with a `rules:` list",
                              file=path)
    for entry in body:
        if not isinstance(entry, Mapping):
            raise ConfigFileError(f"contains a non-mapping rule entry: {entry!r}", file=path)
    return list(body)


def load_rules(directory: str | Path) -> tuple[RewriteRule, ...]:
    """Load and validate every `<rules_dir>/**/*.y[a]ml` into `RewriteRule`s, in `(priority, id)`
    order.

    Files are read in sorted path order and ids must be unique across the whole directory, so the
    returned tuple is a pure function of the directory's *content* — two hosts whose filesystems
    enumerate differently still get the same rules in the same order.
    """
    root = Path(directory)
    if not root.is_dir():
        raise ConfigFileError("is not a rules directory", file=root)
    seen: dict[str, Path] = {}
    rules: list[RewriteRule] = []
    for rule_file in sorted(p for p in root.rglob("*") if p.suffix in {".yaml", ".yml"}):
        for entry in _rules_in_file(rule_file):
            try:
                rule = RewriteRule.model_validate(dict(entry))
            except ValueError as exc:
                raise ConfigValidationError(str(exc), file=rule_file) from exc
            if rule.id in seen:
                raise ConfigValidationError(
                    f"duplicate rule id {rule.id!r}, already defined in {seen[rule.id]}",
                    file=rule_file,
                    key="id",
                )
            seen[rule.id] = rule_file
            rules.append(rule)
    return tuple(sorted(rules, key=rule_sort_key))


class EngineRegistry:
    """`RewriteRule.engine` name → `Rewriter`, the §9 `transform.engines` map made live.

    Open by construction: the registry is built from config, so a fourth engine is a YAML entry
    plus a module, never an edit to this file.
    """

    def __init__(self, rewriters: Iterable[Rewriter]) -> None:
        table: dict[str, Rewriter] = {}
        for rewriter in rewriters:
            if rewriter.engine in table:
                raise ConfigValidationError(
                    f"two rewriters both claim engine name {rewriter.engine!r}", key="engines"
                )
            table[rewriter.engine] = rewriter
        self._table = table

    @classmethod
    def from_modules(cls, engines: Mapping[str, str]) -> EngineRegistry:
        """Import each `name: module` from `transform.engines` and take its `REWRITER`.

        An unimportable module or a module without a `REWRITER` is a startup error: the map is
        configuration, and configuration that names something nonexistent must fail at load, not
        at the first rule that happens to select it.
        """
        rewriters: list[Rewriter] = []
        for name, dotted in sorted(engines.items()):
            try:
                module = importlib.import_module(dotted)
            except ImportError as exc:
                raise UnresolvedReferenceError(
                    f"transform.engines[{name!r}] names module {dotted!r}, which does not "
                    f"import: {exc}",
                    key="transform.engines",
                ) from exc
            rewriter = getattr(module, REWRITER_ATTR, None)
            if rewriter is None:
                raise UnresolvedReferenceError(
                    f"module {dotted!r} exposes no `{REWRITER_ATTR}`, so engine {name!r} "
                    f"resolves to nothing",
                    key="transform.engines",
                )
            if rewriter.engine != name:
                raise ConfigValidationError(
                    f"transform.engines[{name!r}] resolves to a rewriter whose engine is "
                    f"{rewriter.engine!r}",
                    key="transform.engines",
                )
            rewriters.append(rewriter)
        return cls(rewriters)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._table))

    def get(self, engine: str) -> Rewriter | None:
        return self._table.get(engine)

    def require(self, engine: str, *, rule_id: str | None = None) -> Rewriter:
        """Resolve or fail loudly, naming the engine and (when known) the rule that asked."""
        found = self._table.get(engine)
        if found is None:
            who = f"rule {rule_id!r} names" if rule_id else "config names"
            raise UnresolvedReferenceError(
                f"{who} engine {engine!r}, which resolves to no Rewriter "
                f"(have {list(self.names)})",
                key="engine",
            )
        return found

    def validate_rules(self, rules: Sequence[RewriteRule]) -> None:
        """Resolve every rule's engine up front — §7.4's startup-error contract, in one call."""
        for rule in rules:
            self.require(rule.engine, rule_id=rule.id)
