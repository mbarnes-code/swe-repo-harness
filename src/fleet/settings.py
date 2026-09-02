"""`FleetSettings`: the typed configuration layer (SPEC §9).

§9's first sentence is the whole contract: settings load **"in precedence order: CLI flags →
`FLEET_*` environment variables → `config/fleet.yaml` → defaults"**, and **"API keys come from the
environment only"** — a `BackendTarget` names the *variable* (`api_key_env`), never the value, and
a config file containing something that matches a `redaction.patterns` entry is refused at startup
(§9 rule 4, §11.4).

Three files, one object: `config/fleet.yaml` (the run's machinery), `config/repos.yaml` (the fleet
manifest) and `config/models.yaml` (ADR-0023's two-level role→tier→target routing). Every §9 key is
a typed field with §9's default; every section is `extra="forbid"`, so a misspelt key is a startup
error naming the file and the key rather than a default silently taking effect.

**Per-section digests, not one opaque hash.** §6's `runs.config_digests` is `{section: sha256}` and
`runs.config_sha256` is "DERIVED as the hash of the per-section digests below, so the two can never
disagree". That granularity is what makes `fleet resume --accept-drift budgets` (§10) able to accept
exactly one section: a single hash can only ever say "something moved", forcing an operator to
accept every co-edited change in order to accept one. Digests are pure functions of the merged
config — canonical JSON, `sort_keys=True`, no `hash()`, no timestamps — so two processes with
different `PYTHONHASHSEED` agree forever.

**Startup, not lazily.** An unpriced target (§9 rule 5), an unknown `backend`, a rule's `engine`
naming no entry in `transform.engines`, a ladder rung naming no role, a profile that does not exist,
a `concurrency_overrides` entry above `concurrency.llm.*` — each is `ConfigError` with exit code 2,
raised before a repo is touched. §9 rule 5 spells out why the price rule is not pedantry: with no
declared price `budget_ledger.spent_usd` stays `0.00`, `run_max_cost_usd` never trips, and a
250-repo run bills thousands of dollars while §12.24 still passes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, final

import yaml  # type: ignore[import-untyped]  # types-PyYAML is not a dependency
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from fleet.models.enums import ContextPolicy, Ecosystem, EdgeKind, ModelTier, TransformTier
from fleet.models.tasks import BackendTarget, ModelCapabilities, Price
from fleet.vcs.forge import FORGE_NAMES

__all__ = [
    "CONFIG_SECTIONS",
    "ENV_PREFIX",
    "MODELS_SECTION",
    "SHIPPED_BACKENDS",
    "ConfigError",
    "ConfigFileError",
    "ConfigValidationError",
    "FleetConfig",
    "FleetSettings",
    "ModelsConfig",
    "ReposManifest",
    "SecretInConfigError",
    "SecretRegistry",
    "UnpricedTargetError",
    "UnresolvedReferenceError",
    "target_price_usd",
]

type JsonValue = str | int | float | bool | list[Any] | dict[str, Any] | None

ENV_PREFIX: Final = "FLEET_"
ENV_NESTED_DELIMITER: Final = "__"

#: `FLEET_*` names that are switches on the harness, not addresses of a `fleet.yaml` key. Without
#: this set `FLEET_ALLOW_RAW=1` (§9 `redaction.enabled`) would be read as an unknown config key.
RESERVED_ENV: Final[frozenset[str]] = frozenset({"FLEET_ALLOW_RAW"})

#: §6 `runs.config_digests`: "one entry per §10 drift section". Order is part of the contract —
#: `config_sha256` hashes the map, and the map is emitted with sorted keys, so the tuple documents
#: the set rather than the order. `redaction` is present because §9 defines the block; see the
#: module's SPEC-gap note in `FleetSettings.section_digests`.
CONFIG_SECTIONS: Final[tuple[str, ...]] = (
    "run",
    "concurrency",
    "budgets",
    "preflight",
    "redaction",
    "scan",
    "graph",
    "transform",
    "build",
    "stubs",
    "verify",
    "llm",
    "pr",
    "gc",
)

#: The 15th §10 drift section: "plus the resolved `config/models.yaml` profile".
MODELS_SECTION: Final = "models_profile"

#: §7.7's shipped backends. The registry itself lives in `fleet.llm.backends` and is open-ended;
#: settings must not import it (a backend whose SDK is absent does not register, which is not an
#: error until the active profile names it). `FleetSettings.load(known_backends=...)` injects the
#: live registry's keys when there is one; this tuple is the default so the loader can still refuse
#: a typo'd `backend` on a host with no SDKs installed.
SHIPPED_BACKENDS: Final[tuple[str, ...]] = ("anthropic", "openai_compatible", "bedrock", "vertex")

#: Backends that ship behind a `[project.optional-dependencies]` extra (pyproject.toml), mapped to
#: the extra's name. Such a backend failing to register is an uninstalled SDK, NOT a typo — the
#: rule 2 gate names the extra so the operator does not hunt a spelling mistake that is not there.
#: A `SHIPPED_BACKENDS` name ABSENT here ships on a core dependency rather than an extra, which
#: the gate reports differently again. Both halves are bound to pyproject by
#: `test_backend_extras_matches_pyproject` -- edit the manifest and this table together.
_BACKEND_EXTRAS: Final[Mapping[str, str]] = {"bedrock": "bedrock", "vertex": "vertex"}

#: §9 rule 2 / §13 row 36: each backend validates its own target fields.
_REQUIRED_TARGET_FIELDS: Final[Mapping[str, tuple[str, ...]]] = {
    "openai_compatible": ("base_url",),
    "bedrock": ("region",),
    "vertex": ("region",),
}

_DURATION_UNITS: Final[Mapping[str, int]] = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_SIZE_UNITS: Final[Mapping[str, int]] = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


# --------------------------------------------------------------------------------------
# errors — Rule 11: every one names the file and the key
# --------------------------------------------------------------------------------------


class ConfigError(RuntimeError):
    """A startup configuration failure. Exit code 2 ("usage", §10), never a lazy runtime failure.

    Carries `file` and `key` so the message can always answer "which line do I edit"; §10 lists a
    profile with an unpriced target as the canonical exit-2 case.
    """

    exit_code: Final = 2

    def __init__(
        self, message: str, *, file: str | Path | None = None, key: str | None = None
    ) -> None:
        self.file = str(file) if file is not None else None
        self.key = key
        where = ": ".join(p for p in (self.file, self.key) if p)
        super().__init__(f"{where}: {message}" if where else message)


class ConfigFileError(ConfigError):
    """The file is missing, unreadable, or not a YAML mapping."""


class ConfigValidationError(ConfigError):
    """A key is unknown, or its value does not validate against §9's type."""


class UnpricedTargetError(ConfigError):
    """§9 rule 5: a target that declares neither `{in_per_mtok, out_per_mtok}` nor `price: free`."""


class SecretInConfigError(ConfigError):
    """§9 rule 4 / §11.4: config-file text matched a `redaction.patterns` entry.

    The offending value is NEVER included in the message — reporting a leak by quoting it writes
    the secret into the log the redactor exists to protect.
    """


class UnresolvedReferenceError(ConfigError):
    """A name in config resolves to nothing: an unknown `backend`, a `RewriteRule.engine` absent
    from `transform.engines`, a ladder `role` absent from `config/models.yaml`, a missing profile.

    §9's `transform.engines` comment is explicit that this is "a STARTUP error, exactly as an
    unknown `backend` is (§7.4, §7.7, §13 row 36)" — resolving lazily turns a typo into a wave-7
    `KeyError` after hours of spend.
    """


# --------------------------------------------------------------------------------------
# scalar formats §9 writes as strings
# --------------------------------------------------------------------------------------


def parse_duration_s(text: str) -> int:
    """`30d` → 2_592_000. §9's `gc.cache_max_age` is the only duration written this way."""
    match = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", text)
    if match is None:
        raise ValueError(f"{text!r} is not a duration like '30d', '12h', '900s'")
    return int(match.group(1)) * _DURATION_UNITS[match.group(2)]


def parse_size_mb(text: str) -> int:
    """`8g` → 8192. Used for `verify.container_memory` in the §11.3 host-memory arithmetic."""
    match = re.fullmatch(r"\s*(\d+)\s*([kmgb]?)b?\s*", text.lower())
    if match is None:
        raise ValueError(f"{text!r} is not a size like '8g', '512m'")
    return int(match.group(1)) * _SIZE_UNITS[match.group(2)] // (1024**2)


# --------------------------------------------------------------------------------------
# §9 `config/fleet.yaml`
# --------------------------------------------------------------------------------------


class Section(BaseModel):
    """Base for every §9 block. Frozen (settings are read-only once loaded) and `extra="forbid"`
    (an unknown key is a typo the operator must see, not a value silently ignored)."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class RunSection(Section):
    monorepo_path: str = "../acme-monorepo"
    monorepo_branch: str = "integration"
    cache_dir: str = "cache/"          # git mirrors + bazel/dependency caches
    work_dir: str = "work/"            # per-run worktrees
    stale_after_s: int = Field(default=300, gt=0)
    """THE authoritative liveness TTL; a phase captures it verbatim into
    `phases.heartbeat_ttl_seconds` when claimed (§5.5, §6), so the model default and this key are
    the same number by construction."""
    reaper_interval_s: int = Field(default=30, gt=0)
    lease_ttl_s: int = Field(default=300, gt=0)
    projection_hz: int = Field(default=1, gt=0)


class LlmConcurrency(Section):
    """ADR-0023: one semaphore per `ModelTier`, NOT per model or provider."""

    heavy: int = Field(default=2, ge=1)
    workhorse: int = Field(default=8, ge=1)
    cheap: int = Field(default=16, ge=1)

    def for_tier(self, tier: ModelTier) -> int:
        return {ModelTier.HEAVY: self.heavy, ModelTier.WORKHORSE: self.workhorse}.get(
            tier, self.cheap
        )


class ConcurrencySection(Section):
    """§11 semaphore classes."""

    git_net: int = Field(default=8, ge=1)
    subprocess: int = Field(default=16, ge=1)
    docker: int = Field(default=4, ge=1)
    cpu_pool_workers: int = Field(default=8, ge=1)  # ProcessPoolExecutor size (ADR-0003)
    llm: LlmConcurrency = LlmConcurrency()


class TaskWallclock(Section):
    """§11.1: the value `asyncio.timeout()` wraps each worker invocation with, per phase."""

    scan: int = Field(default=600, gt=0)
    transform: int = Field(default=1800, gt=0)
    build: int = Field(default=1800, gt=0)
    verify: int = Field(default=1800, gt=0)
    pr: int = Field(default=600, gt=0)


class BudgetsSection(Section):
    """§11.2; all durable in `budget_ledger` / `repo_ledger`."""

    run_max_cost_usd: float = Field(default=400.0, ge=0.0)      # exit 3, sticky
    wave_max_cost_usd_per_repo: float = Field(default=8.0, ge=0.0)  # × COUNT(wave_members); exit 10
    repo_max_cost_usd: float = Field(default=6.0, ge=0.0)
    repo_max_cost_ceiling_usd: float = Field(default=30.0, ge=0.0)  # cap after blast-radius scaling
    task_max_tokens: int = Field(default=200_000, gt=0)
    wave_max_wallclock_s: int = Field(default=14_400, gt=0)     # exit 4
    wave_drain_timeout_s: int = Field(default=900, ge=0)
    task_max_wallclock_s: TaskWallclock = TaskWallclock()
    max_rss_mb: int = Field(default=4096, gt=0)                 # orchestrator process only
    max_host_rss_mb: int = Field(default=12_288, gt=0)          # whole tree + containers; exit 5
    max_disk_gb: int = Field(default=400, gt=0)                 # exit 9
    build_timeout_s: int = Field(default=1800, gt=0)
    clone_timeout_s: int = Field(default=600, gt=0)


class BaselineBuild(Section):
    """§3.1: the native build/test gate §14.1 claims to have, run before any transformation."""

    enabled: bool = True
    timeout_s: int = Field(default=1800, gt=0)


class PreflightSection(Section):
    """§3.1 step 1; every check is a gate, never a crash."""

    max_repo_bytes: int = Field(default=5_368_709_120, gt=0)     # 5 GiB
    max_blob_bytes: int = Field(default=104_857_600, gt=0)       # 100 MiB
    min_free_bytes: int = Field(default=53_687_091_200, gt=0)    # 50 GiB, re-checked per clone
    unshallow: bool = True
    require_lfs_binary: bool = True
    branch_fallbacks: tuple[str, ...] = ("main", "master", "trunk", "develop")
    baseline_build: BaselineBuild = BaselineBuild()


class RedactionSection(Section):
    """§11.4; applied at EVERY egress boundary, not configurable off."""

    enabled: bool = True
    patterns: dict[str, str] = Field(
        default_factory=lambda: {
            "github_pat": r"github_pat_[A-Za-z0-9_]{20,}",
            "github_classic": r"gh[pousr]_[A-Za-z0-9]{16,}",
            "slack": r"xox[baprs]-[A-Za-z0-9-]{10,}",
            "aws_key": r"AKIA[0-9A-Z]{16}",
            "anthropic": r"sk-ant-[A-Za-z0-9_-]{20,}",
            "openai_style": r"sk-(?!ant-)[A-Za-z0-9]{32,}",
            "gcp_sa_key": r"\"private_key_id\"\s*:\s*\"[a-f0-9]{40}\"",
            "private_key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
            "url_userinfo": r"://[^/\s:@]+:[^/\s@]+@",
        }
    )
    entropy_min_bits: float = Field(default=4.0, ge=0.0)
    entropy_min_len: int = Field(default=20, gt=0)
    replacement: str = "«redacted:{kind}:{fp8}»"
    history_scrub_file: str = "config/rules/secrets.txt"

    @model_validator(mode="after")
    def _patterns_compile(self) -> RedactionSection:
        for kind, pattern in self.patterns.items():
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"redaction.patterns.{kind} is not a valid regex: {exc}") from exc
        return self


class SharedLib(Section):
    """One explicitly declared SHARED_LIB contract (§9 `scan.contracts.shared_libs`)."""

    repo: str
    identifier: str
    paths: tuple[str, ...] = ()
    dest: str | None = None


class ContractsSection(Section):
    """§3.1 step 5b; contract-node discovery (ADR-0019)."""

    enabled: bool = True
    min_consumers: int = Field(default=2, ge=1)
    max_source_dirs: int = Field(default=4, ge=1)
    marker_scan_bytes: int = Field(default=4096, gt=0)
    idl_globs: tuple[str, ...] = ("**/*.proto", "**/*.avsc", "**/*.avdl", "**/*.thrift")
    openapi_roots: tuple[str, ...] = ("openapi", "swagger")
    generated_markers: tuple[str, ...] = (
        "Code generated by protoc",
        "Generated by the protocol buffer compiler",
        "@generated",
        "DO NOT EDIT",
    )
    shared_libs: tuple[SharedLib, ...] = ()
    """The ONLY source of SHARED_LIB contracts; nothing is auto-detected (§9)."""


class ScanSection(Section):
    ignore_globs: tuple[str, ...] = (
        "**/node_modules/**",
        "**/target/**",
        "**/build/**",
        "**/dist/**",
        "**/vendor/**",
        "**/.venv/**",
        "**/testdata/**",
        "**/*.min.js",
    )
    max_file_bytes: int = Field(default=2_097_152, gt=0)
    symbol_batch_rows: int = Field(default=5000, gt=0)
    max_symbols_per_repo: int = Field(default=500_000, gt=0)
    unknown_ecosystem_dest: str = "misc"
    vendor_globs: tuple[str, ...] = (
        "**/vendor/**",
        "**/third_party/**",
        "**/3rdparty/**",
        "**/node_modules/**",
    )
    generated_globs: tuple[str, ...] = (
        "**/generated/**",
        "**/*_pb2.py",
        "**/*.pb.go",
        "**/*_pb.d.ts",
    )
    resource_patterns: dict[str, str] = Field(
        default_factory=lambda: {
            "db_table": r"(?i)\b(?:create|alter)\s+table\s+([a-z0-9_]+)",
            "queue_topic": r"(?i)(?:topic|queue)[\"'\s:=]+([a-z0-9._-]{3,})",
        }
    )
    dynamic_patterns: dict[str, str] = Field(
        default_factory=lambda: {
            "java_reflect": r"Class\.forName\(\s*\"([\w.$]+)\"",
            "py_importlib": r"importlib\.import_module\(\s*[\"']([\w.]+)",
            "js_dynamic": r"(?:require|import)\(\s*[`\"']([^`\"']+)",
            "spring_scan": r"@ComponentScan\([^)]*[\"']([\w.]+)",
        }
    )
    api_contract_patterns: dict[str, str] = Field(
        default_factory=lambda: {
            "grpc_method_path": r"[\"']/((?:[A-Za-z_]\w*\.)+[A-Za-z_]\w*)/[A-Za-z_]\w*[\"']",
        },
        description="A gRPC stub's wire-level RPC path, `/package.Service/Method` — the one "
        "string every generated client emits verbatim regardless of target language. Captures "
        "`package.Service`, the same FQN `_proto_symbols` emits for `GRPC_SERVICE` definitions, "
        "as an `is_definition=False` reference (§12.8, API_CONTRACT).",
    )
    contracts: ContractsSection = ContractsSection()


class GraphSection(Section):
    dag_edge_kinds: tuple[EdgeKind, ...] = (
        EdgeKind.DECLARED_DEP,
        EdgeKind.PUBLISHED_ARTIFACT,
        EdgeKind.INTERNAL_IMPORT,
        EdgeKind.API_CONTRACT,
        EdgeKind.CONTRACT_IMPL,
        EdgeKind.CONTRACT_CONSUME,
    )
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    break_cycles: Literal["auto", "manual"] = "auto"
    hoist_contracts: bool = True
    max_hoists_per_scc: int = Field(default=4, ge=0)
    min_extraction_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    max_breaks_per_scc: int = Field(default=8, ge=0)
    scc_atomic_threshold: int = Field(default=8, ge=1)
    scc_hard_max: int = Field(default=40, ge=1)
    max_edges: int = Field(default=250_000, gt=0)


class LadderRung(Section):
    """ADR-0021: one entry per attempt, in order. `role` resolves through `config/models.yaml` to a
    TIER, so the model stays an ADR-0023 concern and only the CONTEXT is tuned here."""

    tier: TransformTier
    role: str | None = None
    context_policy: ContextPolicy | None = None


class AnchoringSection(Section):
    """ADR-0021; §3.2 step 5."""

    enabled: bool = True
    max_reasks_per_rung: int = Field(default=1, ge=0)
    on_exhausted: Literal["advance", "fail"] = "advance"


_DEFAULT_LADDER: Final[tuple[LadderRung, ...]] = (
    LadderRung(tier=TransformTier.DETERMINISTIC),
    LadderRung(
        tier=TransformTier.LLM_REPAIR,
        role="transform_repair",
        context_policy=ContextPolicy.EVIDENCE_ONLY,
    ),
    LadderRung(
        tier=TransformTier.LLM_ESCALATION,
        role="escalation",
        context_policy=ContextPolicy.EVIDENCE_PLUS_REJECTED_APPROACHES,
    ),
)


class TransformSection(Section):
    rules_dir: str = "config/rules"
    max_attempts: int = Field(default=3, ge=1, le=8)
    """`TransformTask.max_attempts` is `ge=1, le=8` (§5.4): 8 is a sanity rail on config, not
    policy, so raising this is a documented per-run choice."""
    allow_paths_outside_dest: bool = False
    max_patch_bytes: int = Field(default=1_048_576, gt=0)
    stub_blocked: bool = False
    engines: dict[str, str] = Field(
        default_factory=lambda: {
            "ast-grep": "fleet.rewrite.astgrep",
            "libcst": "fleet.rewrite.libcst_py",
            "ts-morph": "fleet.rewrite.tsmorph",
        }
    )
    """§7.4: `RewriteRule.engine` name → Rewriter module. Open by design; a rule naming an engine
    this map does not resolve is a STARTUP error."""
    max_passes: int = Field(default=3, ge=1)
    ladder: tuple[LadderRung, ...] = _DEFAULT_LADDER
    anchoring: AnchoringSection = AnchoringSection()

    @model_validator(mode="after")
    def _ladder_matches_attempts(self) -> TransformSection:
        if len(self.ladder) != self.max_attempts:
            raise ValueError(
                f"transform.ladder has {len(self.ladder)} rungs but max_attempts is "
                f"{self.max_attempts}; §9 requires one entry per attempt"
            )
        first = self.ladder[0]
        if first.tier is not TransformTier.DETERMINISTIC or first.role or first.context_policy:
            raise ValueError("transform.ladder[0] is deterministic and takes no role or policy")
        return self


BCR_DEFAULT_REGISTRY: Final = "https://bcr.bazel.build"
"""Bazel's own built-in module registry — what an unset `build.registry` means."""

BCR_MIRROR_REGISTRY: Final = (
    "https://raw.githubusercontent.com/bazelbuild/bazel-central-registry/main"
)
"""The same Bazel Central Registry, served straight out of its git repository.

Byte-identical content on a different host and a different TLS endpoint, which is the whole
reason it is named here: `bcr.bazel.build` is blocked or blackholed on some networks (it accepts
the socket and then stalls, so Bazel reports `Error accessing registry …: Connect timed out`
rather than a refusal). An operator on such a network sets `build.registry` to this and every
`bazel` command the harness builds carries `--registry=` — the alternative is a fleet whose
`MODULE.bazel` resolution depends on a hostname nobody in the org can reach."""


class BuildSection(Section):
    """§3.3 / ADR-0020. Adapters are code; only their PINS are config."""

    monorepo_dir_overrides: dict[Ecosystem, str] = Field(default_factory=dict)
    registry: str | None = None
    """The Bazel module registry `bazel_dep` / `single_version_override` resolve against.

    `None` means "whatever this Bazel ships with", i.e. `BCR_DEFAULT_REGISTRY` — the honest
    default, because a harness that silently rewrote every operator's registry to a mirror would
    be resolving their pins somewhere they never chose. Set it and `query_argv` /
    `bazel_test_argv` emit `--registry=<url>` on every command, so the registry is a *run-level*
    decision recorded in one config key rather than a `.bazelrc` line each repo could differ on.

    This is load-bearing for verification, not a convenience: `ruleset_versions` above is only a
    real pin if some registry can be reached to resolve it, so on a host that cannot reach BCR
    the choice is this setting or no verification at all. `BCR_MIRROR_REGISTRY` is the known-good
    second address for exactly that case."""

    ruleset_versions: dict[str, str] = Field(
        default_factory=lambda: {
            "rules_jvm_external": "6.7",
            "aspect_rules_js": "3.4.0",
            "aspect_rules_ts": "3.10.0",
            "rules_python": "1.0.0",
            "rules_go": "0.61.1",
            "gazelle": "0.52.2",
            "rules_rust": "0.65.0",
            "rules_proto": "7.1.0",
        }
    )
    """A **pin**, enforced as one: `render_module_bazel` emits `single_version_override` beside
    each `bazel_dep`, so this is the version Bazel SELECTS and not merely the floor it starts
    from. `bazel_dep(version = X)` alone is an MVS minimum — real Bazel resolved a configured
    `rules_python` `1.0.0` to `1.7.0` because a transitive BCR module declared a higher floor,
    which makes §9's "two runs build the same bytes" false for a reason no offline test can see.

    The cost of a real pin is that it is load-bearing: a version that a *newer Bazel* cannot load
    now fails the run instead of being silently raised past. That is the intended trade — an
    operator who must move a ruleset moves it here, in one visible diff.

    **Every version above was chosen by loading it (D8).** Bazel 9 removed the `CcInfo` global,
    the `@local_config_platform` repository and `rule(incompatible_use_toolchain_transition)`, and
    five of the eight pins were on the wrong side of that:

    * `rules_rust@0.54.1` and `rules_go@0.50.1` fail compiling `rust/private/rustdoc_test.bzl` and
      `go/private/rules/cross.bzl` — "The CcInfo symbol has been removed";
    * `gazelle@0.39.1` fails the same way, through `rules_go`;
    * `aspect_rules_ts@3.5.0` floors `aspect_bazel_lib@2.9.3`, whose `platform_utils.bzl` reads
      `@local_config_platform//:constraints.bzl` — "No repository visible as
      '@local_config_platform' from repository '@@aspect_bazel_lib+'";
    * `aspect_rules_js@2.x` *loads* and then fails in ANALYSIS: every 2.x floors
      `rules_nodejs@6.3.x`, whose `nodejs/toolchain.bzl` calls `rule(
      incompatible_use_toolchain_transition = …)`, so `//…:app_bin` "depends on toolchain
      '@@rules_nodejs++node+nodejs_linux_amd64//:toolchain', which cannot be found". No 2.x floors
      a fixed `rules_nodejs`, so the move to 3.x is the only fix that is not a fork.

    Only `rules_rust` and `aspect_rules_ts` were reported; the other three were found by checking
    the whole table, which is why
    `test_bazel.py::test_every_pinned_ruleset_version_loads_under_real_bazel` is parametrized over
    this dict rather than over a list of suspects.

    **This table needs Bazel >= 7.6.0** and is verified on the 9.2.0 that `tools/bin/bazel` runs:
    `aspect_rules_js@3.x` declares `bazel_compatibility = [">=7.6.0"]`, so the 7.4.1 that
    `test_build_e2e.make_monorepo` writes into `.bazelversion` must move with it. Keeping a
    Bazel-7.4.1-compatible table was the alternative and it is not one: it would mean pinning a
    `rules_js` that cannot analyse a single TypeScript target on the Bazel this harness ships."""

    gazelle_binary: str = "gazelle"
    """The BUILD-file generator a `uses_gazelle` adapter delegates to — a **host binary**, looked
    up on `PATH` exactly as `buildverify`'s `bazel_bin` is.

    It used to read `//:gazelle`, which is a Bazel **label** and implies `bazel run //:gazelle`.
    That is not runnable here and never was: the label names a `gazelle` rule in the monorepo's
    root package, nothing in this harness generates one, and `bazel run` of it would put the
    generator behind a full ruleset fetch for every invocation. The generator is also run over a
    **scratch tree** (`cli._run_gazelle`), which is not a Bazel workspace and has no root package
    for such a label to resolve in.

    A bare name rather than a path so an operator can substitute one, and so the workspace's own
    `tools/bin/gazelle` — a *wrapper*, not a symlink, because Gazelle's resolver otherwise picks
    the host's `/usr/bin/go` and writes into `$HOME/go` — is what runs when `tools/bin` is on
    PATH. Its absence is a loud `BuildFileGenerationError`, never a silent skip that would leave
    a Go package with no targets in it and every test still green."""
    fail_on_missing_adapter: bool = True
    openapi_generator: str = "openapi-generator-cli"

    @model_validator(mode="after")
    def _overrides_are_a_bijection(self) -> BuildSection:
        """§9: "An override is validated for uniqueness at startup with the same bijection check as
        the registry itself" — two ecosystems mapped to one directory silently merge two languages'
        trees, which no later stage can un-merge."""
        seen: dict[str, Ecosystem] = {}
        for eco, path in self.monorepo_dir_overrides.items():
            if path in seen:
                raise ValueError(
                    f"build.monorepo_dir_overrides maps both {seen[path].value} and {eco.value} "
                    f"to {path!r}; the ecosystem → dir map must be a bijection"
                )
            seen[path] = eco
        return self


class StubsSection(Section):
    """§3.5.1 — the escape hatch's lifecycle."""

    revalidation: Literal["eager", "batched", "manual"] = "batched"
    max_revalidation_rounds: int = Field(default=2, ge=0)
    revalidation_max_cost_usd: float = Field(default=2.0, ge=0.0)
    on_budget_exhausted: Literal["hold"] = "hold"
    """"hold" is the only value that is not a lie: nothing may move to SUCCEEDED without a green
    against the real dependency (§9), so there is deliberately no `promote`."""


class VerifySection(Section):
    rdeps_limit: int = Field(default=2000, ge=0)
    affected_only: bool = True
    rdeps_sample_n: int = Field(default=500, ge=0)
    disk_cache: str = "cache/bazel/disk"
    repository_cache: str = "cache/bazel/repo"
    container_image: str = "fleet-build:9.2.0-bookworm"
    """The image built by `docker/fleet-build.Dockerfile`, addressed by a LOCAL tag: this fleet
    has no container registry, so a registry-qualified name (the previous
    `ghcr.io/acme/fleet-build:2026-08`) could only ever fail to pull. The tag carries the Bazel
    version the image ships because that is the fact a reader needs and `latest` would hide.

    Build it with:

        docker build -f docker/fleet-build.Dockerfile -t fleet-build:9.2.0-bookworm docker/

    An unbuilt image now fails LOUDLY at the first `docker run` — `Unable to find image … locally`
    then a failed pull, exit 125 — which `BuildverifyWorker._c_toolchain_gate` reports as a
    missing image rather than as a missing compiler."""

    container_memory: str = "8g"
    container_cpus: str = "4.0"
    network: str = "none"


class RateLimitEntry(Section):
    rpm: int = Field(default=0, ge=0)  # 0 = unlimited
    tpm: int = Field(default=0, ge=0)


class AimdSection(Section):
    """The per-TIER LLM semaphore is adaptive, not static (§11.8)."""

    shrink_factor: float = Field(default=0.5, gt=0.0, le=1.0)
    grow_every_s: int = Field(default=60, gt=0)
    floor: int = Field(default=1, ge=1)


class RateLimitSection(Section):
    """§11.8 backpressure. Per TARGET, addressed `<backend>:<model_id>`; an entry absent ⇒
    unlimited, which is the correct default for a local server."""

    honor_retry_after: bool = True
    defaults: RateLimitEntry = RateLimitEntry()
    targets: dict[str, RateLimitEntry] = Field(default_factory=dict)
    aimd: AimdSection = AimdSection()


class FailoverSection(Section):
    """Layered ABOVE §11.8's transient retry, never merged into it."""

    enabled: bool = True
    open_after_failures: int = Field(default=3, ge=1)
    cooldown_s: int = Field(default=120, ge=0)
    max_targets_per_call: int = Field(default=3, ge=1)
    on_tier_exhausted: Literal["halt"] = "halt"  # fail closed, exit 8 (§11.8)


class CapabilityRequirement(Section):
    """§9 rule 3. Deliberately NOT a quality assertion — the harness cannot measure that. It is the
    one mechanical precondition tier-1 work has: a repo's evidence bundle must fit."""

    min_context: int | None = Field(default=None, gt=0)


class LlmSection(Section):
    """ADR-0023. WHICH models answer is `config/models.yaml`; this block is the machinery."""

    profile: str = "default"
    cache_mode: Literal["read-write", "read-only", "off"] = "read-write"
    cache_path: str = "cache/llm"
    max_schema_repairs: int = Field(default=1, ge=0)
    rate_limit: RateLimitSection = RateLimitSection()
    failover: FailoverSection = FailoverSection()
    concurrency_overrides: dict[ModelTier, int] = Field(default_factory=dict)
    require_capabilities: dict[ModelTier, CapabilityRequirement] = Field(
        default_factory=lambda: {ModelTier.HEAVY: CapabilityRequirement(min_context=100_000)}
    )


class PrSection(Section):
    base: str = "integration"
    draft: bool = True
    reviewers_from: str = "owner_hint"
    poll_interval_s: int = Field(default=300, gt=0)
    merge_wait_timeout_s: int = Field(default=172_800, gt=0)
    """48 h from the dependency PR's `opened_at`; on breach the DEPENDENTS go BLOCKED with an
    `UnmergedDependency` finding — reversible by a later `pr_merged` event (§9)."""

    forge: str = "github"
    """Which `vcs.forge.Forge` driver the §3.4 PR path uses. `gh` speaks the GitHub API and cannot
    talk to a self-hosted Gitea, so this selects a DRIVER, not a base URL."""
    forge_url: str = ""
    """Root URL of the instance, e.g. `http://localhost:3001`. Unused by `github` (`gh` resolves
    the remote itself); REQUIRED by `gitea`, whose API path is built from it."""
    forge_owner: str = ""
    """The user or org that owns the monorepo, e.g. `redmage`. Gitea's API path is
    `/repos/{owner}/{repo}/pulls`, so there is no owner to infer."""
    forge_repo: str = ""
    """The monorepo's name on the forge; `owner/name` also accepted. Empty ⇒ every call must pass
    `repo=` explicitly."""
    forge_token_config: str = ".secrets/gitea-curl.conf"  # noqa: S105 - a PATH, not a secret
    """Path (relative to the config root) of the mode-600 `curl -K` file holding the
    `Authorization: token …` header.

    A FILE, not a config value and not an env var, and this is the whole point: `attempts.command`
    persists argv verbatim and `WorkerError.stderr_tail` can quote a command line, so a token
    passed as `-H "Authorization: …"` would be written into the state DB in cleartext — the exact
    leak §11.4 and `obs/redact.py` exist to prevent. `curl` reads it from disk after `execve`, so
    it appears in no argv this harness ever records.
    """

    @model_validator(mode="after")
    def _forge_resolves(self) -> PrSection:
        """An unknown forge, or a Gitea with nothing to talk to, is a STARTUP error.

        Same discipline as an unresolvable §7.4 rewrite engine or an unknown §7.7 backend, and for
        the same reason: `pr.forge: gitlab` discovered at wave 7 is discovered after 250 repos have
        been transformed, with the whole fleet parked at a Phase 4 that cannot open a PR.
        """
        if self.forge not in FORGE_NAMES:
            raise ValueError(
                f"pr.forge {self.forge!r} is not a known forge (have {sorted(FORGE_NAMES)}); "
                "`gh` speaks the GitHub API only, so a forge is a driver, not a URL"
            )
        if self.forge == "gitea":
            required = (("forge_url", self.forge_url), ("forge_owner", self.forge_owner))
            missing = [name for name, value in required if not value.strip()]
            if missing:
                raise ValueError(
                    f"pr.forge is 'gitea' but {', '.join(f'pr.{name}' for name in missing)} "
                    "is empty; Gitea's API path is built from both, and there is nothing to infer"
                )
        return self


class GcSection(Section):
    """§6 retention; only `fleet gc` (§10) deletes, never a running phase."""

    cache_max_age: str = "30d"
    events_keep_runs: int = Field(default=5, ge=0)

    @model_validator(mode="after")
    def _age_parses(self) -> GcSection:
        parse_duration_s(self.cache_max_age)
        return self

    def cache_max_age_s(self) -> int:
        return parse_duration_s(self.cache_max_age)


class FleetConfig(BaseSettings):
    """The merged `config/fleet.yaml` (§9). A `BaseSettings` because this module is the settings
    boundary: §9's precedence (CLI → `FLEET_*` env → file → defaults) is expressed as the source
    order in `_build_config`, not as ad-hoc dictionary merging in a caller."""

    model_config = SettingsConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
        env_prefix=ENV_PREFIX,
        env_nested_delimiter=ENV_NESTED_DELIMITER,
    )

    run: RunSection = RunSection()
    concurrency: ConcurrencySection = ConcurrencySection()
    budgets: BudgetsSection = BudgetsSection()
    preflight: PreflightSection = PreflightSection()
    redaction: RedactionSection = RedactionSection()
    scan: ScanSection = ScanSection()
    graph: GraphSection = GraphSection()
    transform: TransformSection = TransformSection()
    build: BuildSection = BuildSection()
    stubs: StubsSection = StubsSection()
    verify: VerifySection = VerifySection()
    llm: LlmSection = LlmSection()
    pr: PrSection = PrSection()
    gc: GcSection = GcSection()


# --------------------------------------------------------------------------------------
# §9 `config/repos.yaml` and `config/models.yaml`
# --------------------------------------------------------------------------------------


class RepoDefaults(Section):
    ref: str = "main"


class RepoEntry(Section):
    name: str = Field(min_length=1)
    url: str = Field(min_length=1)
    ref: str | None = None
    owns: tuple[str, ...] = ()
    """Optional hint; overrides coordinate AND contract ownership. A contract_id here wins outright
    over the §3.1 5b (iv) ladder."""
    dest: str | None = None
    skip: bool = False  # → RepoStatus.SKIPPED, excluded from the DAG


class ReposManifest(Section):
    """`config/repos.yaml`: the fleet manifest (250 entries of this shape)."""

    version: Literal[1] = 1
    defaults: RepoDefaults = RepoDefaults()
    repos: tuple[RepoEntry, ...] = ()

    @model_validator(mode="after")
    def _names_unique(self) -> ReposManifest:
        seen: set[str] = set()
        for entry in self.repos:
            if entry.name in seen:
                raise ValueError(f"duplicate repo name {entry.name!r}")
            seen.add(entry.name)
        return self

    def ref_for(self, entry: RepoEntry) -> str:
        return entry.ref or self.defaults.ref


class ModelsConfig(Section):
    """`config/models.yaml` (ADR-0023). Two levels, and the separation is the point: `roles` maps a
    role to a capability tier and is essentially fixed; `profiles` maps each tier to an ordered list
    of backend targets and is what an operator actually edits."""

    version: Literal[2] = 2
    roles: dict[str, ModelTier] = Field(default_factory=dict)
    default_profile: str = "default"
    profiles: dict[str, dict[ModelTier, tuple[BackendTarget, ...]]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _default_profile_exists(self) -> ModelsConfig:
        if self.default_profile not in self.profiles:
            raise ValueError(
                f"default_profile {self.default_profile!r} is not one of "
                f"{sorted(self.profiles)}"
            )
        return self


# --------------------------------------------------------------------------------------
# secrets — resolved from the environment, never from a file, never into a digest
# --------------------------------------------------------------------------------------


@final
class SecretRegistry:
    """API keys for the active profile, resolved from `os.environ` by the *name* each target's
    `api_key_env` declares (§9: "API keys come from the environment only").

    The values live behind `SecretStr` and this object's `__repr__` lists only variable NAMES, so a
    settings object caught in a traceback, a `structlog` event or a `model_dump()` cannot carry a
    key. It is deliberately not a Pydantic field of anything: a field would be dumped.
    """

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = {name: SecretStr(value) for name, value in values.items()}

    def __repr__(self) -> str:
        return f"SecretRegistry(names={sorted(self._values)!r})"

    def __contains__(self, env_name: str) -> bool:
        return env_name in self._values

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._values))

    def get(self, env_name: str) -> SecretStr | None:
        return self._values.get(env_name)

    def require(self, env_name: str) -> SecretStr:
        """Fail loud at the point of use, naming the variable the operator must export."""
        secret = self._values.get(env_name)
        if secret is None:
            raise ConfigError(
                f"environment variable {env_name} is unset; §9 reads API keys from the "
                "environment only, and no config file may supply one",
                file="config/models.yaml",
                key="api_key_env",
            )
        return secret


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def target_price_usd(target: BackendTarget, *, in_tokens: int, out_tokens: int) -> float:
    """§11.2's `price(target, n)`, exactly: `(in * n_in + out * n_out) / 1e6`.

    `price: free` is a positive assertion an operator makes about a locally-served target — which is
    exactly what distinguishes "this model costs nothing" from "nobody told the ledger" (§9 rule 5).
    An *omitted* price never reaches this function: it is exit 2 at load.
    """
    price = target.price
    if isinstance(price, Price):
        return (price.in_per_mtok * in_tokens + price.out_per_mtok * out_tokens) / 1e6
    return 0.0


def canonical_json(value: JsonValue) -> str:
    """Deterministic serialization: sorted keys, no incidental whitespace, no `hash()`, no clock."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: JsonValue) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _read_yaml_mapping(path: Path) -> tuple[dict[str, Any], str]:
    """Return `(mapping, raw_text)`. The raw text is kept for the §9 rule 4 secret scan."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigFileError("file not found", file=path) from exc
    except OSError as exc:
        raise ConfigFileError(f"cannot be read: {exc}", file=path) from exc
    try:
        loaded = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigFileError(f"is not valid YAML: {exc}", file=path) from exc
    if loaded is None:
        return {}, raw
    if not isinstance(loaded, dict):
        raise ConfigFileError(
            f"must be a YAML mapping, got {type(loaded).__name__}", file=path
        )
    return {str(k): v for k, v in loaded.items()}, raw


def _dotted(loc: Sequence[str | int]) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out


def _contains_path(data: Mapping[str, Any], loc: Sequence[str | int]) -> bool:
    """Did this source actually supply the key the validator rejected? Used to name the *origin*
    (file, `FLEET_*` var, or CLI flag) in the error rather than blaming the file for an env typo."""
    node: Any = data
    for part in loc:
        if isinstance(part, int):
            if not isinstance(node, list) or part >= len(node):
                return False
            node = node[part]
        else:
            if not isinstance(node, Mapping) or part not in node:
                return False
            node = node[part]
    return True


class _MappingSource(PydanticBaseSettingsSource):
    """One already-materialized layer of §9's precedence chain."""

    def __init__(self, settings_cls: type[BaseSettings], data: Mapping[str, Any]) -> None:
        super().__init__(settings_cls)
        self._data = dict(data)

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._data)


def _build_config(
    *, file_data: Mapping[str, Any], env_data: Mapping[str, Any], cli_data: Mapping[str, Any]
) -> FleetConfig:
    """§9 precedence, expressed as pydantic-settings source order: the earliest source wins, so
    CLI flags beat `FLEET_*` env, which beats `config/fleet.yaml`, which beats the defaults."""

    class _Merged(FleetConfig):
        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            return (
                _MappingSource(settings_cls, cli_data),
                _MappingSource(settings_cls, env_data),
                _MappingSource(settings_cls, file_data),
            )

    # Re-validated into the public class so the object a caller sees (and `repr`s into a log) is a
    # `FleetConfig`, not the private per-load subclass the source order is carried on.
    return FleetConfig.model_validate(_Merged().model_dump())


def _model_for(annotation: object) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def _env_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    """`FLEET_BUDGETS__RUN_MAX_COST_USD=250` → `{"budgets": {"run_max_cost_usd": 250}}`.

    An unknown `FLEET_*` name is an error, not a no-op: an operator who exports
    `FLEET_BUDGET__RUN_MAX_COST_USD` and watches the run spend $400 has been failed silently.
    """
    out: dict[str, Any] = {}
    for name in sorted(env):
        if not name.startswith(ENV_PREFIX) or name in RESERVED_ENV:
            continue
        parts = [p.lower() for p in name[len(ENV_PREFIX) :].split(ENV_NESTED_DELIMITER) if p]
        if not parts:
            continue
        model: type[BaseModel] | None = FleetConfig
        node = out
        for depth, part in enumerate(parts):
            leaf = depth == len(parts) - 1
            if model is None:
                raise ConfigValidationError(
                    f"{name} addresses a sub-key of a scalar; set the whole value as JSON instead",
                    file=f"env:{name}",
                    key=".".join(parts[:depth]),
                )
            field_info = model.model_fields.get(part)
            if field_info is None:
                raise ConfigValidationError(
                    f"{name} names no key in config/fleet.yaml "
                    f"(no {'.'.join(parts[: depth + 1])})",
                    file=f"env:{name}",
                    key=".".join(parts[: depth + 1]),
                )
            if leaf:
                node[part] = _coerce_env_value(env[name])
            else:
                node = node.setdefault(part, {})
                model = _model_for(field_info.annotation)
    return out


def _coerce_env_value(raw: str) -> Any:
    """Env values are strings. JSON first (so lists, maps and numbers round-trip), raw otherwise —
    `container_memory=8g` is not JSON and must stay the string §9 documents."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _nest(overrides: Mapping[str, Any]) -> dict[str, Any]:
    """`{"budgets.run_max_cost_usd": 250}` → nested dicts: the shape `fleet --set a.b=c` gives."""
    out: dict[str, Any] = {}
    for dotted, value in overrides.items():
        parts = dotted.split(".")
        node = out
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise ConfigValidationError(
                    f"CLI override {dotted!r} conflicts with another override", key=dotted
                )
            node = child
        node[parts[-1]] = value
    return out


# --------------------------------------------------------------------------------------
# the loader
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FleetSettings:
    """Validated run configuration. Constructed once per process, passed down explicitly.

    Holds no open handles and no mutable state: it is a value, and `section_digests` is what a
    resume compares against `runs.config_digests` section by section (§6, §10).
    """

    config: FleetConfig
    repos: ReposManifest
    models: ModelsConfig
    profile: str
    """The ACTIVE profile: `llm.profile`, which `--profile` overrides (§10)."""
    root: Path
    config_dir: Path
    section_digests: Mapping[str, str]
    secrets: SecretRegistry = field(repr=False)
    """Excluded from `repr` twice over: the registry masks itself, and the dataclass never prints
    it. Two guards, because one of them being removed by a later edit must not leak a key."""

    # ---------------- loading ----------------

    @classmethod
    def load(
        cls,
        config_dir: str | Path = "config",
        *,
        cli_overrides: Mapping[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
        root: str | Path | None = None,
        known_backends: Iterable[str] | None = None,
        capabilities: Callable[[BackendTarget], ModelCapabilities] | None = None,
    ) -> FleetSettings:
        """Load and fully validate `fleet.yaml`, `repos.yaml` and `models.yaml`.

        `cli_overrides` are dotted keys (`{"budgets.run_max_cost_usd": 250}`) and win over
        `FLEET_*` env, which wins over the file, which wins over §9's defaults. `known_backends`
        injects the live §7.7 registry (Guardrail 3: settings depends on a name set, not on
        `fleet.llm`); `capabilities` likewise injects declared capabilities for the §9 rule 3 gate.

        Raises `ConfigError` (exit 2) — never returns a partially validated object.
        """
        environ = dict(os.environ if env is None else env)
        cfg_dir = Path(config_dir)
        root_dir = Path(root) if root is not None else cfg_dir.parent

        fleet_path = cfg_dir / "fleet.yaml"
        repos_path = cfg_dir / "repos.yaml"
        models_path = cfg_dir / "models.yaml"

        file_data, fleet_raw = _read_yaml_mapping(fleet_path)
        env_data = _env_overrides(environ)
        cli_data = _nest(cli_overrides or {})

        config = cls._validate_fleet_config(
            fleet_path, file_data=file_data, env_data=env_data, cli_data=cli_data
        )

        repos_data, repos_raw = _read_yaml_mapping(repos_path)
        models_data, models_raw = _read_yaml_mapping(models_path)

        # §9 rule 4: the scan uses the MERGED redaction patterns, so it runs after fleet.yaml
        # validates and covers fleet.yaml itself.
        sources = ((fleet_path, fleet_raw), (repos_path, repos_raw), (models_path, models_raw))
        for path, raw in sources:
            _refuse_secret_material(path, raw, config.redaction.patterns)

        _check_redaction_switch(config, environ, fleet_path)

        repos = _validate_model(ReposManifest, repos_data, repos_path)
        models = _validate_models_config(models_path, models_data)

        profile = config.llm.profile
        if profile not in models.profiles:
            raise UnresolvedReferenceError(
                f"llm.profile {profile!r} names no profile in config/models.yaml "
                f"(have {sorted(models.profiles)})",
                file=fleet_path,
                key="llm.profile",
            )

        backends = tuple(known_backends) if known_backends is not None else SHIPPED_BACKENDS
        _check_routing(config, models, profile, models_path, backends, capabilities)
        _check_rule_engines(config, root_dir)
        _check_concurrency_overrides(config, fleet_path)

        digests = _section_digests(config, models, profile)
        secrets = _resolve_secrets(models, profile, environ)
        return cls(
            config=config,
            repos=repos,
            models=models,
            profile=profile,
            root=root_dir,
            config_dir=cfg_dir,
            section_digests=digests,
            secrets=secrets,
        )

    @staticmethod
    def _validate_fleet_config(
        path: Path,
        *,
        file_data: Mapping[str, Any],
        env_data: Mapping[str, Any],
        cli_data: Mapping[str, Any],
    ) -> FleetConfig:
        try:
            return _build_config(file_data=file_data, env_data=env_data, cli_data=cli_data)
        except ValidationError as exc:
            first = exc.errors()[0]
            loc = first["loc"]
            origin: str | Path
            if _contains_path(cli_data, loc):
                origin = "cli:--set"
            elif _contains_path(env_data, loc):
                origin = f"env:{ENV_PREFIX}{ENV_NESTED_DELIMITER.join(str(p) for p in loc).upper()}"
            else:
                origin = path
            raise ConfigValidationError(
                first["msg"], file=origin, key=_dotted(loc)
            ) from exc

    # ---------------- digests ----------------

    def config_sha256(self) -> str:
        """§6: "DERIVED as the hash of the per-section digests below, so the two can never disagree
        about what the config was"."""
        return _digest(dict(self.section_digests))

    def section_digest(self, section: str) -> str:
        try:
            return self.section_digests[section]
        except KeyError as exc:
            raise KeyError(
                f"{section!r} is not a config digest section; have "
                f"{sorted(self.section_digests)}"
            ) from exc

    def drifted_sections(self, baseline: Mapping[str, str]) -> tuple[str, ...]:
        """Which sections moved since `runs.config_digests` was written — the list
        `fleet resume --accept-drift SECTION` accepts one member of at a time (§10).

        An EMPTY baseline is "no per-section baseline was recorded" (§6's back-fill), which is never
        read as "every section matches": the caller falls back to the whole-config comparison.
        """
        if not baseline:
            return tuple(sorted(self.section_digests))
        return tuple(
            sorted(
                name
                for name in set(baseline) | set(self.section_digests)
                if baseline.get(name) != self.section_digests.get(name)
            )
        )

    # ---------------- routing accessors ----------------

    def tier_for_role(self, role: str) -> ModelTier:
        tier = self.models.roles.get(role)
        if tier is None:
            raise UnresolvedReferenceError(
                f"role {role!r} is not declared in `roles` (have {sorted(self.models.roles)})",
                file=self.config_dir / "models.yaml",
                key="roles",
            )
        return tier

    def targets_for_tier(self, tier: ModelTier) -> tuple[BackendTarget, ...]:
        return self.models.profiles[self.profile].get(tier, ())

    def llm_concurrency(self, tier: ModelTier) -> int:
        """`concurrency.llm.*` as lowered by `llm.concurrency_overrides` — the one place a local
        deployment's throughput is tuned (§9). Raising above the base is refused at startup."""
        base = self.config.concurrency.llm.for_tier(tier)
        return min(base, self.config.llm.concurrency_overrides.get(tier, base))

    # ---------------- §11.3 host memory ----------------

    def memory_commitment_mb(self) -> int:
        """`concurrency.docker × verify.container_memory + budgets.max_rss_mb` (§11.3).

        Exposed rather than enforced at load: §9's OWN defaults breach the rule it states
        (4 × 8 GiB + 4 GiB = 36 GiB against `max_host_rss_mb: 12288`), so enforcing it here would
        make the shipped configuration unloadable. The caller that also knows the host's MemTotal
        decides — see `validate_memory_budget`.
        """
        per_container = parse_size_mb(self.config.verify.container_memory)
        return self.config.concurrency.docker * per_container + self.config.budgets.max_rss_mb

    def validate_memory_budget(self, *, host_total_mb: int | None = None) -> None:
        """The §9 check, for a caller that has decided to enforce it. Raises `ConfigError`."""
        commitment = self.memory_commitment_mb()
        for limit, label in ((self.config.budgets.max_host_rss_mb, "budgets.max_host_rss_mb"),
                             (host_total_mb, "the host's MemTotal")):
            if limit is not None and commitment > limit:
                raise ConfigError(
                    f"concurrency.docker × verify.container_memory + budgets.max_rss_mb = "
                    f"{commitment} MiB exceeds {label} ({limit} MiB)",
                    file=self.config_dir / "fleet.yaml",
                    key="budgets.max_host_rss_mb",
                )


# --------------------------------------------------------------------------------------
# startup checks
# --------------------------------------------------------------------------------------


def _validate_model[M: BaseModel](model: type[M], data: Mapping[str, Any], path: Path) -> M:
    try:
        return model.model_validate(dict(data))
    except ValidationError as exc:
        first = exc.errors()[0]
        raise ConfigValidationError(first["msg"], file=path, key=_dotted(first["loc"])) from exc


def _refuse_secret_material(path: Path, raw: str, patterns: Mapping[str, str]) -> None:
    """§9 rule 4 / §11.4. The matched text is never echoed: only the pattern name and the line."""
    for kind, pattern in patterns.items():
        match = re.search(pattern, raw)
        if match is None:
            continue
        line = raw.count("\n", 0, match.start()) + 1
        raise SecretInConfigError(
            f"line {line} matches the `{kind}` redaction pattern; §9 accepts only `api_key_env` "
            "— the NAME of an environment variable, never a key",
            file=path,
            key=f"redaction.patterns.{kind}",
        )


def _check_redaction_switch(config: FleetConfig, env: Mapping[str, str], path: Path) -> None:
    """§9: "setting this false is refused at startup unless FLEET_ALLOW_RAW=1"."""
    if not config.redaction.enabled and env.get("FLEET_ALLOW_RAW") != "1":
        raise ConfigValidationError(
            "redaction may not be disabled; export FLEET_ALLOW_RAW=1 to override (§11.4)",
            file=path,
            key="redaction.enabled",
        )


def _validate_models_config(path: Path, data: Mapping[str, Any]) -> ModelsConfig:
    """Parse `config/models.yaml` target by target, so §9 rule 5's error can name the profile, the
    tier and the target index instead of a Pydantic loc buried in a 12-profile file."""
    version = data.get("version")
    if version is not None and version != 2:
        raise ConfigValidationError(
            f"version {version!r} is not supported: ADR-0023 requires the two-level "
            "`roles` (role → ModelTier) + `profiles` (tier → ordered targets) shape, version 2",
            file=path,
            key="version",
        )
    profiles = data.get("profiles")
    if isinstance(profiles, Mapping):
        for profile_name, tiers in profiles.items():
            if not isinstance(tiers, Mapping):
                continue
            for tier_name, targets in tiers.items():
                for index, target in enumerate(targets if isinstance(targets, list) else []):
                    if isinstance(target, Mapping) and "price" not in target:
                        raise UnpricedTargetError(
                            "declares no `price`: §9 rule 5 requires either "
                            "`{in_per_mtok, out_per_mtok}` or the literal `price: free`. Without "
                            "one the ledger prices every call at $0.00, `run_max_cost_usd` never "
                            "trips, and the run bills unbounded dollars while §12.24 still passes",
                            file=path,
                            key=(
                                f"profiles.{profile_name}.{tier_name}[{index}] "
                                f"({target.get('backend', '?')}:{target.get('model_id', '?')})"
                            ),
                        )
    return _validate_model(ModelsConfig, data, path)


def _check_routing(
    config: FleetConfig,
    models: ModelsConfig,
    profile: str,
    path: Path,
    known_backends: Sequence[str],
    capabilities: Callable[[BackendTarget], ModelCapabilities] | None,
) -> None:
    """§9 rules 1–3, plus the ladder's `role` references. Every failure is a startup error."""
    tiers = models.profiles[profile]

    # Rule 1: every tier named by a role has a non-empty target list in the selected profile.
    for role, tier in sorted(models.roles.items()):
        if not tiers.get(tier):
            raise UnresolvedReferenceError(
                f"role {role!r} routes to tier {tier.value}, which has no targets in profile "
                f"{profile!r}; a role routed to an empty tier is a startup error, never a runtime "
                "KeyError in wave 7",
                file=path,
                key=f"profiles.{profile}.{tier.value}",
            )

    # The ADR-0021 ladder names roles; an unknown one is unresolvable here, not at attempt 2.
    for index, rung in enumerate(config.transform.ladder):
        if rung.role is not None and rung.role not in models.roles:
            raise UnresolvedReferenceError(
                f"transform.ladder[{index}].role {rung.role!r} is not declared in "
                f"config/models.yaml `roles` (have {sorted(models.roles)})",
                file=path,
                key="roles",
            )

    # Rule 2: every `backend` resolves, and each backend's own required fields are present.
    for tier, targets in sorted(tiers.items()):
        for index, target in enumerate(targets):
            where = f"profiles.{profile}.{tier.value}[{index}]"
            if target.backend not in known_backends:
                # Three distinguishable causes, and the operator's next action differs for each.
                # Collapsing them into "install the extra" sends the two non-extra cases hunting a
                # `fleet[...]` extra that does not exist.
                extra = _BACKEND_EXTRAS.get(target.backend)
                if extra is not None:                       # shipped behind an optional extra
                    remedy = (
                        f"it ships as an optional extra whose SDK is not installed on this "
                        f"host — `pip install 'fleet[{extra}]'`"
                    )
                elif target.backend in SHIPPED_BACKENDS:    # core dependency; import must be broken
                    remedy = (
                        "it ships as a CORE dependency, so this is not a missing extra — its "
                        "module failed to import on this host; check the install"
                    )
                else:   # not a name we ship at all: a typo, or a backend some out-of-tree
                        # module was expected to have registered
                    remedy = "if it ships as an extra, install it"
                raise UnresolvedReferenceError(
                    f"backend {target.backend!r} is not in the §7.7 registry "
                    f"(have {sorted(known_backends)}); {remedy}",
                    file=path,
                    key=where,
                )
            for required in _REQUIRED_TARGET_FIELDS.get(target.backend, ()):
                value = getattr(target, required)
                # `base_url: ''` is not a base_url. An `is None` test here let an empty or
                # whitespace-only string through startup and deferred the failure to the first
                # call, which §13 row 36 exists to prevent: it must fail HERE, naming the field.
                if value is None or (isinstance(value, str) and not value.strip()):
                    raise ConfigValidationError(
                        f"backend {target.backend!r} requires `{required}` (§13 row 36)",
                        file=path,
                        key=f"{where}.{required}",
                    )
            _reject_unknown_capability_keys(target, path, where)

    # Rule 3: `llm.require_capabilities` is met by the FIRST target of each named tier.
    for tier, requirement in sorted(config.llm.require_capabilities.items()):
        targets = tiers.get(tier, ())
        if not targets or requirement.min_context is None:
            continue
        first = targets[0]
        declared = capabilities(first).max_context if capabilities is not None else None
        override = first.capabilities_override.get("max_context")
        max_context = override if isinstance(override, int) else declared
        if max_context is not None and max_context < requirement.min_context:
            raise ConfigValidationError(
                f"tier {tier.value} requires min_context {requirement.min_context} but its first "
                f"target {first.backend}:{first.model_id} declares max_context {max_context} "
                "(§9 rule 3, §13 row 38)",
                file=path,
                key=f"profiles.{profile}.{tier.value}[0]",
            )


def _reject_unknown_capability_keys(target: BackendTarget, path: Path, where: str) -> None:
    """`capabilities_override` is merged into `ModelCapabilities`; a key that model has no field
    for would be merged into nothing and silently believed."""
    unknown = sorted(set(target.capabilities_override) - set(ModelCapabilities.model_fields))
    if unknown:
        raise ConfigValidationError(
            f"capabilities_override names no ModelCapabilities field: {unknown}",
            file=path,
            key=f"{where}.capabilities_override",
        )


def _iter_rule_engines(path: Path) -> Iterator[tuple[str, str]]:
    """Yield `(rule_id, engine)` for every rule in a `transform.rules_dir` file. Only `engine` is
    read here — the rule's full schema is §7.4's business, not the settings layer's."""
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigFileError(f"is not a readable rule file: {exc}", file=path) from exc
    rules = loaded.get("rules") if isinstance(loaded, dict) else loaded
    if not isinstance(rules, list):
        return
    for index, rule in enumerate(rules):
        if isinstance(rule, Mapping) and "engine" in rule:
            yield str(rule.get("rule_id", rule.get("id", index))), str(rule["engine"])


def _check_rule_engines(config: FleetConfig, root: Path) -> None:
    """§9 `transform.engines`: "An engine a rule names and this map does not resolve is a STARTUP
    error, exactly as an unknown `backend` is"."""
    rules_dir = root / config.transform.rules_dir
    if not rules_dir.is_dir():
        return
    for rule_file in sorted(p for p in rules_dir.rglob("*") if p.suffix in {".yaml", ".yml"}):
        for rule_id, engine in _iter_rule_engines(rule_file):
            if engine not in config.transform.engines:
                raise UnresolvedReferenceError(
                    f"rule {rule_id!r} names engine {engine!r}, which transform.engines does not "
                    f"map to a Rewriter module (have {sorted(config.transform.engines)})",
                    file=rule_file,
                    key="engine",
                )


def _check_concurrency_overrides(config: FleetConfig, path: Path) -> None:
    """§9: "Raising above `concurrency.llm.*` is refused at startup"."""
    for tier, value in sorted(config.llm.concurrency_overrides.items()):
        base = config.concurrency.llm.for_tier(tier)
        if value > base:
            raise ConfigValidationError(
                f"llm.concurrency_overrides.{tier.value} = {value} raises the tier semaphore above "
                f"concurrency.llm ({base}); overrides may only LOWER it",
                file=path,
                key=f"llm.concurrency_overrides.{tier.value}",
            )


def _resolve_secrets(
    models: ModelsConfig, profile: str, env: Mapping[str, str]
) -> SecretRegistry:
    """Read every `api_key_env` the ACTIVE profile names, from the environment, once.

    A variable that is unset is not an error here — a local OpenAI-compatible server legitimately
    wants a dummy key, and §9 states no requirement that every named variable exist. It fails loud
    at the point of use, via `SecretRegistry.require`.
    """
    names = {
        target.api_key_env
        for targets in models.profiles[profile].values()
        for target in targets
        if target.api_key_env
    }
    return SecretRegistry({name: env[name] for name in sorted(names) if name in env})


def _section_digests(config: FleetConfig, models: ModelsConfig, profile: str) -> dict[str, str]:
    """`{section: sha256}` over the MERGED config (§6 `runs.config_digests`).

    `mode="json"` first, so enums and paths are primitives and the payload is exactly what a future
    process re-derives. Nothing here reads a clock, an id, or `hash()`. No secret can enter: the
    only key material in scope is `api_key_env`, which is a variable NAME.
    """
    dumped = config.model_dump(mode="json")
    digests = {name: _digest(dumped[name]) for name in CONFIG_SECTIONS}
    resolved = {
        "profile": profile,
        "roles": {role: tier.value for role, tier in models.roles.items()},
        "targets": {
            tier.value: [target.model_dump(mode="json") for target in targets]
            for tier, targets in models.profiles[profile].items()
        },
    }
    digests[MODELS_SECTION] = _digest(resolved)
    return digests
