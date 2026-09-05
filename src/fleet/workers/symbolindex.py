"""Phase 1 step 4: the cross-repo symbol index (§3.1 step 4, Constraint 4).

Exported definitions, imports, protobuf `service`/`message` names, and the resource/dynamic
string literals `graph/infer.py` reads — extracted one file at a time and returned as
`SymbolRef` rows for the single writer to persist (§11.5).

**Bounded by construction, in three independent ways** (§11.3), because "index a 250-repo fleet"
is the one place in Phase 1 where an unbounded list is a real out-of-memory:

* the unit of work handed to `ctx.limits.cpu_pool` is **one file path**, never a tree and never
  file bytes: the child opens, parses, and returns that file's rows;
* an invocation returns at most `symbol_batch_rows` rows. When the batch fills it stops with
  `status='partial'`, the runner persists the batch as the checkpoint, and re-entry resumes at
  `remaining_units`. No `list[SymbolRef]` for a whole repo is ever resident;
* a file over `max_file_bytes` is skipped with a `FileTooLarge` finding, and a repo past
  `max_symbols_per_repo` stops with `SymbolBudgetExceeded` and a deliberately partial index.

**The deadline is checked between files, and that is what `partial` is for.** A worker that ran
out of wall clock with 400 of 900 files indexed has 400 real rows; reporting `failed` would make
the next attempt re-parse all 900 and re-insert the 400, which is the duplicate-row defect the
five-valued status exists to prevent.

Extraction is deliberately **stdlib-only** — `ast` for Python, anchored regexes for everything
else. §3.1 names tree-sitter, and `tree_sitter` is not a declared dependency of this harness
(`pyproject.toml`), so importing it would be a runtime failure dressed as a citation. The
`FileScan` boundary is the seam a tree-sitter backend replaces: paths in, `SymbolRef`s out.
"""

from __future__ import annotations

import ast
import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import ClassVar, Final

from pydantic import Field

from fleet.models.enums import FailureClass, Phase, SymbolKind
from fleet.models.graph import SymbolRef
from fleet.models.repo import RepoId
from fleet.orchestrator.registry import register_worker
from fleet.workers.base import (
    BaseWorker,
    WorkerContext,
    WorkerError,
    WorkerInput,
    WorkerOutput,
    WorkerResult,
    loop_now,
)
from fleet.workers.interrogate import (
    DEFAULT_IGNORE_GLOBS,
    checkpointed_units,
    owed_from,
    paths_intact,
    walk_files,
    worktree_of,
    worktree_presence,
)

__all__ = [
    "LANGUAGES",
    "FileScan",
    "SymbolIndexInput",
    "SymbolIndexOutput",
    "SymbolindexWorker",
    "scan_file",
]

LANGUAGES: Final[Mapping[str, str]] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".proto": "proto",
    ".avsc": "avro",
    ".avdl": "avro",
    ".thrift": "thrift",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
}
"""Suffix → language. A file whose suffix is absent is not a unit at all: the unit list has to be
derivable identically on every re-entry, and "everything, then skip most of it" makes the
checkpoint's `remaining_units` depend on what the extractor happened to understand that day."""

DEFAULT_RESOURCE_PATTERNS: Final[Mapping[str, str]] = {
    "db_table": r"(?i)\b(?:create|alter)\s+table\s+([a-z0-9_]+)",
    "queue_topic": r"(?i)(?:topic|queue)[\"'\s:=]+([a-z0-9._-]{3,})",
}
DEFAULT_DYNAMIC_PATTERNS: Final[Mapping[str, str]] = {
    "java_reflect": r"Class\.forName\(\s*\"([\w.$]+)\"",
    "py_importlib": r"importlib\.import_module\(\s*[\"']([\w.]+)",
    "js_dynamic": r"(?:require|import)\(\s*[`\"']([^`\"']+)",
    "spring_scan": r"@ComponentScan\([^)]*[\"']([\w.]+)",
}
DEFAULT_API_CONTRACT_PATTERNS: Final[Mapping[str, str]] = {
    "grpc_method_path": r"[\"']/((?:[A-Za-z_]\w*\.)+[A-Za-z_]\w*)/[A-Za-z_]\w*[\"']",
    "http_path_template": (
        r"(?<![fFrRbB])[\"'`](/(?:[\w.-]+/)*\{[A-Za-z_]\w*\}(?:/(?:[\w.-]+|\{[A-Za-z_]\w*\}))*)"
        r"[\"'`]"
    ),
}
r"""A gRPC stub's wire-level RPC path, `/package.Service/Method` — this is the ONE string every
generated client emits verbatim regardless of target language (it is the HTTP/2 path gRPC's wire
protocol itself uses, not a codegen convention), which is what makes a single regex honest here
where a codegen-import regex would not be: `channel.unary_unary('/acme.billing.v1.Billing/
Charge', ...)` in a Python `*_pb2_grpc.py` stub, and the same literal shape in generated-JS/TS
(`grpc-js`) and Go stubs (a `_FullMethodName` constant). This scanner only indexes
Python/TS/JS/JSON/YAML/proto/avro/thrift files (`_units()`'s `LANGUAGES`) — Go and Java stubs are
never scan units regardless, and the claim is not extended to Java: grpc-java builds the full
method name at runtime via `generateFullMethodName(SERVICE_NAME, "Method")`, concatenating two
literals rather
than embedding the joined string, so this pattern would not match a Java stub even if `.java` were
a scan unit. The captured group is exactly `package.Service` — the same FQN `_proto_symbols` emits
for `GRPC_SERVICE`, so no reshaping is needed for `_api_contract_edges`'s fqn-equality join.

`http_path_template` — an OpenAPI path template embedded in a quoted/backtick string literal,
`/widgets/{widgetId}` shaped. Round VI research-29 ran real `openapi-generator-cli` generation
(python, typescript-fetch, typescript-axios) and found this literal — brace placeholders and
all — survives byte-identical across all three generators and matches the OpenAPI spec's own
`paths:` key exactly; it is load-bearing for correctness (a client cannot address the right
resource without embedding it), not a stylistic convention, the same honesty bar
`grpc_method_path` above meets. Unlike that pattern's distinctive dotted-FQN-then-bare-method
shape, a bare `/`-prefixed string is NOT inherently distinctive — ordinary URL literals, log
messages, and file paths are a real false-positive risk (CRITERIA_PLAN §8) — so this pattern is
deliberately NARROWER than "any multi-segment REST-shaped path": it requires at least one
`{param}`-style placeholder segment (the specific shape research-29 verified, not merely "looks
path-like"), and it excludes any literal immediately preceded by a Python string-prefix letter
(`f`/`F`/`r`/`R`/`b`/`B`) via a negative lookbehind on the opening quote — ruling out the single
most plausible false positive this project's own scan units can produce: a Python f-string like
`f"/tmp/jobs/{job_id}/status"` building a dynamic log/file path, which is lexically
indistinguishable from a real path template except for that prefix. A JS/TS template-literal
interpolation, `` `/api/users/${userId}` ``, is excluded structurally rather than by lookbehind:
its placeholder segment starts with `$`, which matches neither this pattern's literal-segment
class (`[\w.-]+`) nor its placeholder class (`\{...\}` must immediately follow a `/`), so the
match simply fails to form. The captured group is the path template exactly as written (leading
slash and braces kept verbatim) — the same normalized form the OpenAPI `paths:` definition-side
extractor (`_openapi_path_symbols`) emits, so no reshaping is needed for the fqn-equality join
either.

**Disclosed false-positive class this pattern does NOT rule out (task review, round VI task
51 fix round 1):** the `(?<![fFrRbB])` lookbehind only excludes an f/r/b-STRING-PREFIXED literal.
Any BARE, non-prefixed quoted string containing a `{name}`-shaped segment still matches, whether
or not it has anything to do with an HTTP client — e.g. `log_dir_template =
"/var/log/app/{date}/access.log"` (a log/file-path template with a brace placeholder), or a
same-repo Flask-style route REGISTRATION, `app.add_url_rule('/widgets/{widgetId}', ...)` (a
`{name}` placeholder is also Flask's own bare-string route-parameter syntax, not just OpenAPI's).
Cache keys, cron schedule paths, and S3 key templates in the same brace-placeholder shape are the
same class. This is accepted, not patched, for the same reason gRPC's own Java limitation and
task 46's content-hash-collision risk were accepted rather than closed: `_api_contract_edges`
requires an EXACT fqn match against a real `is_definition=True` `HTTP_OPERATION` symbol somewhere
else in the fleet, so a coincidental literal collision (this pattern firing on unrelated code
whose brace-templated string happens to be byte-identical to a real OpenAPI path some OTHER repo
defines) is needed to produce a wrong edge — tightening the regex further to close this class
risks under-matching real generated-client code, which is the harder failure mode to detect.
"""
DEFAULT_GENERATED_MARKERS: Final[tuple[str, ...]] = (
    "Code generated by protoc",
    "Generated by the protocol buffer compiler",
    "@generated",
    "DO NOT EDIT",
)

MARKER_SCAN_BYTES: Final = 4096
"""`scan.contracts.marker_scan_bytes`: how much of a file's head can claim it is generated."""

_TS_EXPORT = re.compile(
    r"^\s*export\s+(?:default\s+)?(?:async\s+)?"
    r"(class|interface|type|enum|function|const|let)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)
_TS_IMPORT = re.compile(
    r"^\s*(?:import|export)\s[^;\n]*?from\s*[\"']([^\"']+)[\"']|require\(\s*[\"']([^\"']+)[\"']",
    re.MULTILINE,
)
_PROTO_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
_PROTO_SERVICE = re.compile(r"^\s*service\s+(\w+)", re.MULTILINE)
_PROTO_MESSAGE = re.compile(r"^\s*message\s+(\w+)", re.MULTILINE)
_PROTO_IMPORT = re.compile(r"^\s*import\s+(?:public\s+|weak\s+)?\"([^\"]+)\"", re.MULTILINE)
_AVSC_NAMESPACE = re.compile(r'"namespace"\s*:\s*"([^"]+)"')
_AVDL_NAMESPACE = re.compile(r'@namespace\(\s*"([^"]+)"\s*\)')
_THRIFT_NAMESPACE = re.compile(r"^\s*namespace\s+(\*|[A-Za-z]\w*)\s+([\w.]+)", re.MULTILINE)
_YAML_ROOT_KEY = re.compile(r"^([A-Za-z_][\w.-]*)\s*:", re.MULTILINE)
_JSON_ROOT_KEY = re.compile(r"^\s*\"([^\"]+)\"\s*:", re.MULTILINE)

_TS_KINDS: Final[Mapping[str, SymbolKind]] = {
    "class": SymbolKind.CLASS,
    "interface": SymbolKind.INTERFACE,
    "type": SymbolKind.INTERFACE,
    "enum": SymbolKind.CLASS,
    "function": SymbolKind.FUNCTION,
    "const": SymbolKind.FUNCTION,
    "let": SymbolKind.FUNCTION,
}


@dataclass(frozen=True, slots=True)
class FileScan:
    """One file's extraction result. Crosses the process-pool boundary, so: data only.

    `error` rather than an exception, because a raise inside a pool child arrives at the parent
    stripped of the path that caused it — and a syntax error in one file is a finding about that
    file, never a reason to abandon a repo's index.
    """

    symbols: list[SymbolRef] = field(default_factory=list)
    error: str | None = None
    skipped: bool = False


class SymbolIndexInput(WorkerInput):
    repo_id: RepoId
    worktree_path: str | None = None
    ignore_globs: tuple[str, ...] = DEFAULT_IGNORE_GLOBS
    max_file_bytes: int = Field(default=2_097_152, gt=0)
    symbol_batch_rows: int = Field(
        default=5000, gt=0, description="§3.1 step 4: rows per batch, and per invocation"
    )
    max_symbols_per_repo: int = Field(default=500_000, gt=0)
    symbols_already_indexed: int = Field(
        default=0, ge=0, description="Rows landed by earlier re-entries; the budget is per REPO"
    )
    resource_patterns: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_RESOURCE_PATTERNS)
    )
    dynamic_patterns: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_DYNAMIC_PATTERNS)
    )
    api_contract_patterns: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_API_CONTRACT_PATTERNS)
    )
    generated_markers: tuple[str, ...] = DEFAULT_GENERATED_MARKERS
    remaining_units: tuple[str, ...] | None = Field(
        default=None, description="The checkpoint's owed file paths; None = index the tree whole"
    )
    completed_units: tuple[str, ...] = Field(
        default=(),
        description="The checkpoint's landed paths, when the phase's factory supplies them; "
        "empty falls back to `all_units - remaining_units` (see `interrogate.completed_from`)",
    )


class SymbolIndexOutput(WorkerOutput):
    """One batch of `symbols` rows, plus what the batch cost and what it could not read."""

    repo_id: RepoId
    symbols: tuple[SymbolRef, ...] = ()
    files_indexed: int = Field(default=0, ge=0)
    truncated: bool = Field(
        default=False, description="`max_symbols_per_repo` hit: edges from this repo cap at 0.6"
    )
    findings: tuple[str, ...] = ()


@register_worker
class SymbolindexWorker(BaseWorker[SymbolIndexInput, SymbolIndexOutput]):
    """Per-file symbol extraction on `ctx.limits.cpu_pool`; paths in, rows out (ADR-0003)."""

    __slots__ = ()

    name: ClassVar[str] = "symbolindex"
    phase: ClassVar[Phase] = Phase.SCAN
    input_model: ClassVar[type[WorkerInput]] = SymbolIndexInput
    output_model: ClassVar[type[WorkerOutput]] = SymbolIndexOutput

    async def preconditions_hold(
        self, ctx: WorkerContext, payload: SymbolIndexInput
    ) -> bool:
        """Is a prior index still describing the tree that is on disk right now?

        The checkpoint names files, so the check is about files: every unit the checkpoint counts
        as indexed must still exist. A deleted or reaped source file means the persisted rows
        describe something that is gone, and re-entering incrementally would leave the index
        permanently disagreeing with the worktree — so it answers False and the phase re-runs
        from `phases.base_ref`. Fresh work (no checkpoint) is False for the same reason it is
        everywhere else: there is nothing to resume.
        """
        if payload.remaining_units is None:
            return False
        root = worktree_of(ctx, payload.worktree_path)
        if not await asyncio.to_thread(root.is_dir):
            return False
        units = await asyncio.to_thread(self._units, root, payload.ignore_globs)
        done = checkpointed_units(units, payload.remaining_units, payload.completed_units)
        return await asyncio.to_thread(paths_intact, root, done)

    async def run(
        self, ctx: WorkerContext, payload: SymbolIndexInput
    ) -> WorkerResult[SymbolIndexOutput]:
        root = worktree_of(ctx, payload.worktree_path)
        presence = await asyncio.to_thread(worktree_presence, root)
        if isinstance(presence, OSError):
            return WorkerResult[SymbolIndexOutput](
                status="failed",
                error=WorkerError(
                    failure_class=FailureClass.TRANSIENT_INFRA,
                    retryable=True,
                    stderr_tail=(
                        f"could not determine whether worktree {root} exists: {presence}"
                    ),
                ),
            )
        if not presence:
            return WorkerResult[SymbolIndexOutput](
                status="failed",
                error=WorkerError(
                    failure_class=FailureClass.PREFLIGHT,
                    retryable=False,
                    stderr_tail=f"worktree {root} does not exist; run the clone worker first",
                ),
            )

        units = await asyncio.to_thread(self._units, root, payload.ignore_globs)
        owed = owed_from(units, payload.remaining_units)
        landed: list[str] = []
        symbols: list[SymbolRef] = []
        findings: list[str] = []
        truncated = False
        loop = asyncio.get_running_loop()

        for index, rel in enumerate(owed):
            if ctx.cancelled() or ctx.expired(loop_now()):
                return self._interrupted(payload, landed, owed[index:], symbols, findings)
            if payload.symbols_already_indexed + len(symbols) >= payload.max_symbols_per_repo:
                findings.append("SymbolBudgetExceeded")
                truncated = True
                break
            scan = await loop.run_in_executor(
                ctx.limits.cpu_pool,
                scan_file,
                str(root),
                rel,
                payload.repo_id,
                payload.max_file_bytes,
                dict(payload.resource_patterns),
                dict(payload.dynamic_patterns),
                dict(payload.api_contract_patterns),
                tuple(payload.generated_markers),
            )
            landed.append(rel)
            if scan.skipped:
                findings.append(f"FileTooLarge:{rel}")
            if scan.error is not None:
                findings.append(f"ParseFailed:{rel}")
            symbols.extend(scan.symbols)
            if len(symbols) >= payload.symbol_batch_rows and index + 1 < len(owed):
                # The batch is full: hand it over rather than grow it (§11.3). This is a `partial`
                # with everything it landed, so the next dispatch starts at the next file.
                return self._interrupted(payload, landed, owed[index + 1 :], symbols, findings)

        return WorkerResult[SymbolIndexOutput](
            status="ok",
            output=SymbolIndexOutput(
                repo_id=payload.repo_id,
                symbols=tuple(symbols),
                files_indexed=len(landed),
                truncated=truncated,
                findings=tuple(findings),
            ),
            completed_units=list(
                dict.fromkeys(
                    checkpointed_units(units, payload.remaining_units, payload.completed_units)
                    + landed
                )
            ),
            evidence=landed[:32],
        )

    # -- internals -----------------------------------------------------------------------

    def _units(self, root: Path, ignore_globs: Sequence[str]) -> list[str]:
        """The indexable files, sorted — the SAME list on every re-entry, which is what makes
        `remaining_units` mean anything."""
        return [
            rel for rel in walk_files(root, ignore_globs) if Path(rel).suffix.lower() in LANGUAGES
        ]

    def _interrupted(
        self,
        payload: SymbolIndexInput,
        landed: Sequence[str],
        remaining: Sequence[str],
        symbols: Sequence[SymbolRef],
        findings: Sequence[str],
    ) -> WorkerResult[SymbolIndexOutput]:
        """Stopped between files: deadline, cancel, or a full batch.

        The rows already extracted travel WITH the `partial` — they are the checkpoint's whole
        point. Nothing landed means nothing to resume from, and that is `cancelled`, because
        `partial` with an empty `completed_units` is `failed` under a friendlier name.
        """
        if not landed:
            return WorkerResult[SymbolIndexOutput](
                status="cancelled",
                error=WorkerError(
                    failure_class=FailureClass.TIMEOUT,
                    retryable=True,
                    stderr_tail="stopped before any file was indexed",
                ),
            )
        return WorkerResult[SymbolIndexOutput](
            status="partial",
            output=SymbolIndexOutput(
                repo_id=payload.repo_id,
                symbols=tuple(symbols),
                files_indexed=len(landed),
                findings=tuple(findings),
            ),
            completed_units=list(landed),
            remaining_units=list(remaining),
        )


# -------------------------------------------------------------------------------------------
# extraction — pure functions, so the pool child needs nothing but a path (ADR-0003)
# -------------------------------------------------------------------------------------------


def scan_file(
    root: str,
    rel: str,
    repo_id: str,
    max_file_bytes: int,
    resource_patterns: Mapping[str, str],
    dynamic_patterns: Mapping[str, str],
    api_contract_patterns: Mapping[str, str],
    generated_markers: Sequence[str],
) -> FileScan:
    """Extract one file's symbols. Runs in a pool child: arguments and result are plain data.

    Never raises. A file that cannot be read, or that the parser refuses, comes back as a named
    `error` so the caller can record a finding against that path and keep indexing the repo.
    """
    path = Path(root) / rel
    try:
        size = path.stat().st_size
        if size > max_file_bytes:
            return FileScan(skipped=True)
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return FileScan(error=str(exc))

    language = LANGUAGES.get(path.suffix.lower(), "text")
    symbols: list[SymbolRef] = []
    error: str | None = None

    if language == "python":
        parsed, error = _python_symbols(text, repo_id, rel)
        symbols.extend(parsed)
    elif language in ("typescript", "javascript"):
        symbols.extend(_ts_symbols(text, repo_id, rel, language))
    elif language == "proto":
        symbols.extend(_proto_symbols(text, repo_id, rel))
    elif language == "avro":
        symbols.extend(_avro_symbols(text, repo_id, rel, path.suffix.lower()))
    elif language == "thrift":
        symbols.extend(_thrift_symbols(text, repo_id, rel))
    elif language in ("yaml", "json"):
        root_symbols = _document_root_keys(text, repo_id, rel, language)
        symbols.extend(root_symbols)
        if _is_openapi_document(root_symbols, rel):
            symbols.extend(_openapi_path_symbols(text, repo_id, rel, language))

    symbols.extend(_pattern_symbols(text, repo_id, rel, language, resource_patterns))
    symbols.extend(
        _pattern_symbols(
            text, repo_id, rel, language, dynamic_patterns, kind=SymbolKind.DYNAMIC_REF
        )
    )
    symbols.extend(
        _pattern_symbols(
            text, repo_id, rel, language, api_contract_patterns, kind_map=_API_CONTRACT_KINDS
        )
    )
    if _looks_generated(text, generated_markers):
        symbols.append(
            _symbol(repo_id, f"{rel}#generated", SymbolKind.MODULE, rel, 1, language, False)
        )
    return FileScan(symbols=symbols, error=error)


def _symbol(
    repo_id: str,
    fqn: str,
    kind: SymbolKind,
    rel: str,
    line: int,
    language: str,
    is_definition: bool,
    *,
    exported: bool = False,
) -> SymbolRef:
    return SymbolRef(
        repo_id=repo_id,
        fqn=fqn,
        kind=kind,
        path=rel,
        line=max(1, line),
        language=language,
        is_definition=is_definition,
        exported=exported,
    )


def _module_prefix(rel: str) -> str:
    """`src/acme/billing.py` → `src.acme.billing`: the dotted form an import would name."""
    stem = re.sub(r"\.pyi?$", "", rel)
    return stem.replace("/", ".").removesuffix(".__init__")


def _python_symbols(text: str, repo_id: str, rel: str) -> tuple[list[SymbolRef], str | None]:
    """Definitions and imports via `ast` — a real parser, so a decorator or an f-string cannot
    fake a definition the way a regex over Python source can."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        return [], f"{type(exc).__name__}: {exc}"

    prefix = _module_prefix(rel)
    out: list[SymbolRef] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            kind = SymbolKind.CLASS if isinstance(node, ast.ClassDef) else SymbolKind.FUNCTION
            out.append(
                _symbol(
                    repo_id,
                    f"{prefix}.{node.name}",
                    kind,
                    rel,
                    node.lineno,
                    "python",
                    True,
                    exported=not node.name.startswith("_"),
                )
            )
    for imported in ast.walk(tree):
        if isinstance(imported, ast.Import):
            for alias in imported.names:
                out.append(
                    _symbol(
                        repo_id, alias.name, SymbolKind.IMPORT, rel,
                        imported.lineno, "python", False,
                    )
                )
        elif isinstance(imported, ast.ImportFrom) and imported.module:
            out.append(
                _symbol(
                    repo_id, imported.module, SymbolKind.IMPORT, rel,
                    imported.lineno, "python", False,
                )
            )
    return out, None


def _ts_symbols(text: str, repo_id: str, rel: str, language: str) -> list[SymbolRef]:
    """`export`ed declarations and module specifiers. Only EXPORTED declarations are recorded:
    an unexported local cannot be the far end of a cross-repo edge, and indexing it would put
    ~10⁵ private names per repo into the table §11.3 is trying to keep bounded."""
    prefix = re.sub(r"\.[jt]sx?$|\.mjs$", "", rel).replace("/", ".")
    out: list[SymbolRef] = []
    for match in _TS_EXPORT.finditer(text):
        keyword, name = match.group(1), match.group(2)
        out.append(
            _symbol(
                repo_id,
                f"{prefix}.{name}",
                _TS_KINDS.get(keyword, SymbolKind.FUNCTION),
                rel,
                _line_of(text, match.start()),
                language,
                True,
                exported=True,
            )
        )
    for match in _TS_IMPORT.finditer(text):
        target = match.group(1) or match.group(2)
        if target:
            out.append(
                _symbol(
                    repo_id, target, SymbolKind.IMPORT, rel,
                    _line_of(text, match.start()), language, False,
                )
            )
    return out


def _proto_symbols(text: str, repo_id: str, rel: str) -> list[SymbolRef]:
    """`package` (the contract identity §3.1 5b keys on), plus services and messages under it."""
    package_match = _PROTO_PACKAGE.search(text)
    package = package_match.group(1) if package_match else ""
    out: list[SymbolRef] = []
    if package_match:
        out.append(
            _symbol(
                repo_id, package, SymbolKind.MODULE, rel,
                _line_of(text, package_match.start()), "proto", True, exported=True,
            )
        )
    for pattern, kind in ((_PROTO_SERVICE, SymbolKind.GRPC_SERVICE),
                          (_PROTO_MESSAGE, SymbolKind.PROTO_MESSAGE)):
        for match in pattern.finditer(text):
            name = match.group(1)
            fqn = f"{package}.{name}" if package else name
            out.append(
                _symbol(
                    repo_id, fqn, kind, rel, _line_of(text, match.start()), "proto", True,
                    exported=True,
                )
            )
    for match in _PROTO_IMPORT.finditer(text):
        out.append(
            _symbol(
                repo_id, match.group(1), SymbolKind.IMPORT, rel,
                _line_of(text, match.start()), "proto", False,
            )
        )
    return out


def _avro_symbols(text: str, repo_id: str, rel: str, suffix: str) -> list[SymbolRef]:
    """§3.1 5b (i): AVRO's identifier is the schema's `namespace` field/annotation, alone — not
    `namespace.name` — mirroring `.proto`'s identifier being the bare `package`."""
    pattern = _AVSC_NAMESPACE if suffix == ".avsc" else _AVDL_NAMESPACE
    match = pattern.search(text)
    if not match:
        return []
    namespace = match.group(1)
    return [
        _symbol(
            repo_id, namespace, SymbolKind.MODULE, rel,
            _line_of(text, match.start()), "avro", True, exported=True,
        )
    ]


def _thrift_symbols(text: str, repo_id: str, rel: str) -> list[SymbolRef]:
    """§3.1 5b (i): THRIFT's identifier is the namespace directive — the `*` (all-languages) slot
    if present, else the lexicographically first per-language slot's value."""
    slots = {m.group(1): (m.group(2), m.start()) for m in _THRIFT_NAMESPACE.finditer(text)}
    if not slots:
        return []
    scope = "*" if "*" in slots else min(slots)
    value, start = slots[scope]
    return [
        _symbol(
            repo_id, value, SymbolKind.MODULE, rel,
            _line_of(text, start), "thrift", True, exported=True,
        )
    ]


def _document_root_keys(text: str, repo_id: str, rel: str, language: str) -> list[SymbolRef]:
    """A YAML/JSON document's ROOT mapping keys, recorded while the file is already open.

    §3.1 5b needs exactly this to spot an `openapi:`/`swagger:` document and must "open no file
    of its own". Column-anchored rather than parsed: a real YAML load of an arbitrary repo file
    is unbounded work on untrusted input, and the question here is only which keys sit at the
    document root.
    """
    pattern = _JSON_ROOT_KEY if language == "json" else _YAML_ROOT_KEY
    out: list[SymbolRef] = []
    seen: set[str] = set()
    for match in pattern.finditer(text):
        if language == "json" and _depth_at(text, match.start()) != 1:
            continue
        key = match.group(1)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            _symbol(
                repo_id, f"{rel}#{key}", SymbolKind.MODULE, rel,
                _line_of(text, match.start()), language, True,
            )
        )
    return out


def _is_openapi_document(root_symbols: Sequence[SymbolRef], rel: str) -> bool:
    """§3.1 5b's `openapi:`/`swagger:` root-key signal, read from `_document_root_keys`'s own
    result rather than re-scanning the text a second time — this is the gate that stops
    `_openapi_path_symbols` from ever running against an arbitrary YAML/JSON file's own
    unrelated `paths:` key."""
    markers = {f"{rel}#openapi", f"{rel}#swagger"}
    return any(sym.fqn in markers for sym in root_symbols)


def _openapi_path_symbols(text: str, repo_id: str, rel: str, language: str) -> list[SymbolRef]:
    """OpenAPI/Swagger `paths:` block keys -> `HTTP_OPERATION` DEFINITIONS (§12.8's last
    `EdgeKind`, `API_CONTRACT`). Column-anchored, not a parse — same design principle as
    `_document_root_keys` and `_proto_symbols`: a full YAML/JSON load of untrusted repo content
    is unbounded work, and the question here is only which keys sit directly under the document's
    `paths:` mapping. The caller gates this on `_is_openapi_document`, so a `paths:` key in some
    unrelated document can never reach here.

    The FQN is the path template exactly as written — leading slash and `{param}` braces kept
    verbatim, no reshaping — because research-29 found this exact literal survives byte-identical
    across `openapi-generator-cli`'s python/typescript-fetch/typescript-axios output and matches
    the spec's own `paths:` key. That is the same normalized form `http_path_template`'s
    consumer-side regex captures (`DEFAULT_API_CONTRACT_PATTERNS` above), so no reshaping happens
    on either side of `_api_contract_edges`'s fqn-equality join.
    """
    if language == "json":
        return _openapi_json_path_symbols(text, repo_id, rel)
    return _openapi_yaml_path_symbols(text, repo_id, rel)


_YAML_PATHS_ROOT = re.compile(r"^paths:[ \t]*(?:#.*)?$", re.MULTILINE)
_YAML_ROOT_LEVEL_LINE = re.compile(r"^\S", re.MULTILINE)
_YAML_PATH_KEY_LINE = re.compile(
    r"^([ \t]+)[\"']?(/[^\"'\s:]*)[\"']?:[ \t]*(?:#.*)?$", re.MULTILINE
)


def _openapi_yaml_path_symbols(text: str, repo_id: str, rel: str) -> list[SymbolRef]:
    root = _YAML_PATHS_ROOT.search(text)
    if root is None:
        return []
    end_match = _YAML_ROOT_LEVEL_LINE.search(text, root.end())
    block_end = end_match.start() if end_match else len(text)
    block = text[root.end() : block_end]
    out: list[SymbolRef] = []
    child_indent: str | None = None
    for match in _YAML_PATH_KEY_LINE.finditer(block):
        indent = match.group(1)
        if child_indent is None:
            child_indent = indent
        elif indent != child_indent:
            continue  # a deeper-nested `/`-shaped line (e.g. a callback key) — not a direct child
        out.append(
            _symbol(
                repo_id, match.group(2), SymbolKind.HTTP_OPERATION, rel,
                _line_of(text, root.end() + match.start()), "yaml", True, exported=True,
            )
        )
    return out


_JSON_PATHS_KEY = re.compile(r"^\s*\"paths\"\s*:", re.MULTILINE)


def _openapi_json_path_symbols(text: str, repo_id: str, rel: str) -> list[SymbolRef]:
    match = _JSON_PATHS_KEY.search(text)
    if match is None:
        return []
    open_idx = text.find("{", match.end())
    if open_idx == -1:
        return []
    close_idx = _matching_brace(text, open_idx)
    if close_idx == -1:
        return []
    block = text[open_idx:close_idx]
    out: list[SymbolRef] = []
    for key_match in _JSON_ROOT_KEY.finditer(block):
        if _depth_at(block, key_match.start()) != 1:
            continue
        key = key_match.group(1)
        if not key.startswith("/"):
            continue
        out.append(
            _symbol(
                repo_id, key, SymbolKind.HTTP_OPERATION, rel,
                _line_of(text, open_idx + key_match.start()), "json", True, exported=True,
            )
        )
    return out


def _matching_brace(text: str, open_idx: int) -> int:
    """Index of the `{` at `open_idx`'s matching `}`, ignoring braces inside quoted strings — the
    same quote-aware counting `_depth_at` uses, run forward from a known opening brace instead of
    accumulating from the document start."""
    depth = 0
    in_string = False
    escaped = False
    for i in range(open_idx, len(text)):
        char = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


@lru_cache(maxsize=64)
def _compiled(pattern: str) -> re.Pattern[str] | None:
    """Compile a config-supplied pattern once. A bad pattern disables ITS rule and nothing else —
    an operator's typo in `resource_patterns` must not take the fleet's symbol index down."""
    try:
        return re.compile(pattern)
    except re.error:
        return None


def _pattern_symbols(
    text: str,
    repo_id: str,
    rel: str,
    language: str,
    patterns: Mapping[str, str],
    *,
    kind: SymbolKind | None = None,
    kind_map: Mapping[str, SymbolKind] | None = None,
) -> list[SymbolRef]:
    """`scan.resource_patterns` / `scan.dynamic_patterns` / `scan.api_contract_patterns` — the
    string literals a compiler cannot see (§3.1 steps 4 and 5). Always references, never
    definitions: a topic name in a source file is evidence that the repo TOUCHES it, never that it
    owns it.

    Per-name kind resolution: when `kind` is omitted, each pattern NAME resolves its own
    `SymbolKind` via `kind_map` (default `_RESOURCE_KINDS`, so `resource_patterns`' existing
    `db_table`/`queue_topic` split is unchanged) — this is what lets `api_contract_patterns`
    carry two categories (`grpc_method_path` -> `GRPC_SERVICE`, `http_path_template` ->
    `HTTP_OPERATION`) in one dict without mislabeling either."""
    effective_map = kind_map if kind_map is not None else _RESOURCE_KINDS
    out: list[SymbolRef] = []
    for name, raw in sorted(patterns.items()):
        compiled = _compiled(raw)
        if compiled is None:
            continue
        resolved = kind if kind is not None else effective_map.get(name, SymbolKind.QUEUE_TOPIC)
        for match in compiled.finditer(text):
            value = match.group(1) if match.groups() else match.group(0)
            out.append(
                _symbol(
                    repo_id, value, resolved, rel, _line_of(text, match.start()), language, False
                )
            )
    return out


_RESOURCE_KINDS: Final[Mapping[str, SymbolKind]] = {
    "db_table": SymbolKind.DB_TABLE,
    "queue_topic": SymbolKind.QUEUE_TOPIC,
}
_API_CONTRACT_KINDS: Final[Mapping[str, SymbolKind]] = {
    "grpc_method_path": SymbolKind.GRPC_SERVICE,
    "http_path_template": SymbolKind.HTTP_OPERATION,
}


def _looks_generated(text: str, markers: Sequence[str]) -> bool:
    """§3.1 5b (ii): a generated-code marker in the file's head. Evidence of consumption, never
    of ownership — which is what stops twelve repos carrying `*_pb2.py` from each claiming the
    contract."""
    head = text[:MARKER_SCAN_BYTES]
    return any(marker in head for marker in markers)


def _line_of(text: str, offset: int) -> int:
    """1-based line number of a match offset. `SymbolRef.line` is `ge=1`, and an off-by-one here
    is a `ValidationError` at insert time rather than a wrong number in a table."""
    return text.count("\n", 0, offset) + 1


def _depth_at(text: str, offset: int) -> int:
    """Brace depth before `offset` — how a JSON key is told apart from a nested one without
    parsing the document. Quoted braces are not counted; a string containing one would otherwise
    hide every key after it."""
    depth = 0
    in_string = False
    escaped = False
    for char in text[:offset]:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
    return depth
