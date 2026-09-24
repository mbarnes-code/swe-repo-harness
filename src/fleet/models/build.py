"""Build emission models exchanged with `bazel/emit.py` (SPEC §5.6, ADR-0020)."""

from __future__ import annotations

from typing import Final, Literal

from pydantic import Field, model_validator

from fleet.models.base import FleetModel
from fleet.models.enums import ContractKind, Ecosystem  # noqa: F401  (ContractKind per §5.6)
from fleet.models.graph import ContractId
from fleet.models.repo import Coordinate, RepoId

BAZEL_IDENTIFIER_PATTERN: Final = r"^[a-zA-Z_][a-zA-Z0-9_]*$"
"""The closed shape for any bare, unescaped identifier position in generated BUILD.bazel /
MODULE.bazel Starlark text -- currently the rule keyword head (`BuildTarget.rule`,
`BuildTargetProposal.rule` in `llm/schemas.py`), and reused for `render_target()`'s attribute
names / `_split_extension()`'s var+tag per SECURITY_REVIEW.md item #6. Every real Bazel rule name
this codebase's ecosystem adapters emit already satisfies it (`java_library`, `ts_project`,
`go_test`, `filegroup`, ...); it exists to close a Starlark-injection primitive at the schema
boundary, not to restrict which real rules can be emitted."""


class InternalDep(FleetModel):
    """One FIRST-PARTY dependency of a unit: a sibling repo that is migrating into this monorepo.

    A bare label was not enough, and that shortfall is the whole of D12. `internal_deps` used to
    be `list[str]` — `["//ts/acme/lib:lib"]` — which says where the sibling's Bazel target is and
    nothing about what the sibling *is*. Every ruleset that links first-party packages needs the
    other two facts: `npm_translate_lock` resolves a `link:<dir>` entry in the lockfile and links
    it under the package's registry **name**, so `JsAdapter` cannot emit either without the
    sibling's `dest` and its published `Coordinate`. Deriving them from the label is not possible
    (`//ts/acme/lib:lib` does not spell `@acme/lib`) and guessing them is how `@acme/lib` and
    `@corp/lib` become one package.

    The label still comes from the driver, which owns the internal/external split (§3.3 step 2) —
    the adapter is handed the resolved facts and decides only how its language spells them.
    """

    label: str = Field(min_length=1, description="'//<dest>:<name>', resolved by the driver")
    dest: str = Field(min_length=1, description="The sibling's monorepo dir; layout() output")
    published: Coordinate | None = Field(
        default=None,
        description="The sibling's primary published coordinate. `None` for a sibling that "
        "publishes nothing, which is exactly the case where no registry name exists to link it "
        "under — a first-party link is then not expressible and the plain label is all there is.",
    )


class BuildUnit(FleetModel):
    """The one thing handed to an adapter for target generation: a repo OR a hoisted contract's
    owning slice, already relocated. Adapters never touch RepoRecord, ContractNode, or the DB."""

    unit_id: RepoId | ContractId
    ecosystem: Ecosystem = Field(
        description="The PRIMARY published coordinate's ecosystem — the one that decided `dest` "
        "(§3.3). A polyglot repo is one unit in one directory, not one unit per language; "
        "secondary-language sources are `srcs` of targets the primary adapter emits."
    )
    dest: str = Field(description="Monorepo-relative POSIX dir; layout() output (§3.3)")
    srcs: list[str] = Field(default_factory=list, description="dest-relative source paths")
    test_srcs: list[str] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=list)
    published: Coordinate | None = Field(default=None, description="Primary published coordinate")
    internal_deps: list[InternalDep] = Field(
        default_factory=list,
        description="Already-resolved first-party siblings: label, dest and published coordinate",
    )
    external_coordinates: list[Coordinate] = Field(
        default_factory=list, description="Deps that stay external; drive workspace_deps()"
    )
    contract_deps: list[ContractId] = Field(
        default_factory=list, description="Hoisted contracts consumed; ADR-0019 binding labels"
    )


class BuildTarget(FleetModel):
    """One rule instance in a generated BUILD.bazel. Rendered verbatim; never hand-edited."""

    package: str = Field(description="Monorepo-relative package dir, i.e. BuildUnit.dest")
    name: str = Field(min_length=1)
    rule: str = Field(
        min_length=1,
        pattern=BAZEL_IDENTIFIER_PATTERN,
        description="e.g. java_library, ts_project, go_test, filegroup. Pattern-constrained "
        "(SECURITY_REVIEW.md item #6): rendered verbatim as the head of a Starlark function call "
        "in generated BUILD.bazel, which `bazel build` evaluates.",
    )
    load_from: str | None = Field(
        default=None, description="bzl label for the load() stmt; None for native rules"
    )
    srcs: list[str] = Field(default_factory=list)
    deps: list[str] = Field(default_factory=list, description="Bazel labels, internal or external")
    attrs: dict[str, str | int | bool | list[str]] = Field(default_factory=dict)
    testonly: bool = False
    visibility: list[str] = Field(default_factory=lambda: ["//visibility:private"])
    """Private by default (ADR-0144): at 250-repo scale one shared visibility namespace made
    every silently-defaulted target — every internal helper, every non-test target nobody
    intended as a cross-repo surface — `//visibility:public`, the opposite of the style guide's
    "scope tightly" guidance. A unit's dependency-surface target (the one `deps=[...]` actually
    reference, per `ecosystems/base.target_name`) sets this explicitly to
    `["//visibility:public"]` at its own construction site; every other target either inherits
    this private default or sets its own explicit visibility — never the model default silently
    deciding a reader's question."""

    @property
    def label(self) -> str:
        return f"//{self.package}:{self.name}"


class WorkspaceDep(FleetModel):
    """One external dependency as MODULE.bazel must express it. The single point at which
    maven.install / npm_translate_lock / pip.parse / crate.from_cargo / go_deps differ."""

    ruleset: str = Field(description="bazel_dep name, e.g. rules_jvm_external; version from §9")
    extension: str = Field(description="use_extension id, e.g. maven.install, pip.parse")
    coordinate: Coordinate
    resolved_version: str | None = Field(
        default=None, description="MVS winner; None until §3.3 step 3 reconciliation"
    )
    repo_name: str = Field(description="The @repo name consumers reference, e.g. maven, npm, pypi")
    attrs: dict[str, str | list[str]] = Field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"@{self.repo_name}//:{self.coordinate.name}"


class GazelleConfig(FleetModel):
    """Emitted instead of BuildTargets when EcosystemAdapter.uses_gazelle is true (§3.3 step 2)."""

    directives: list[str] = Field(
        default_factory=list, description="'# gazelle:' lines written to the package BUILD file"
    )
    prefix: str | None = Field(default=None, description="gazelle:prefix, e.g. the go module path")
    exclude: list[str] = Field(default_factory=list)
    args: list[str] = Field(
        default_factory=list,
        description="The FLAGS the generator must be invoked with, and nothing positional. The "
        "driver supplies the binary, `-repo_root` and the directories to visit — the roots are a "
        "fleet-wide fact (a cross-repo import resolves to a real in-repo label only when every "
        "Go root is on one command line) and no single unit can know them. Every unit of one "
        "ecosystem declares the identical flags, which is what lets the driver collapse them "
        "into one invocation; a unit that disagrees with its siblings is a loud refusal.",
    )


class ToolchainRequirement(FleetModel):
    """What MODULE.bazel must register for this adapter's targets to build hermetically."""

    ruleset: str
    extension: str = Field(description="e.g. java_toolchains.toolchain, python.toolchain")
    name: str
    version: str
    attrs: dict[str, str | list[str]] = Field(
        default_factory=dict,
        description="The tag call's attributes, rendered VERBATIM — this is the ruleset's own "
        "dialect, so `version` above is only a pin until an adapter spells it the way its tag "
        "class does. `str | list[str]`, matching `WorkspaceDep.attrs`, because a version "
        "attribute is not always scalar: `rust.toolchain` takes `versions`, a `string_list`, and "
        "a `dict[str, str]` cannot hold `[\"1.81.0\"]` at all — which is how Rust's pin came to "
        "be declared and never emitted.",
    )
    repo_names: list[str] = Field(
        default_factory=list,
        description="Repos this extension tag creates that MODULE.bazel must bring into scope "
        "with `use_repo`. A LIST, not one name: a single tag routinely creates several importable "
        "repos (rules_python's `python.toolchain` creates `python_3_12`, `python_3_12_host` and "
        "`pythons_hub` from one call), and a repo the root module never imports is `no such "
        "package '@@[unknown repo …]'` at load time. Which names a ruleset exports is per-ruleset "
        "knowledge, so the adapter that names the extension fills this in (§13 row 6).",
    )


class SupportFile(FleetModel):
    """A file the generated `MODULE.bazel`/`BUILD.bazel` **names**, and that therefore has to be
    on disk before Bazel reads them (D10).

    Emission and creation were two different phases with no contract between them: `pip.parse`
    named `//:requirements.lock`, `npm_translate_lock` named `//:pnpm-lock.yaml` and
    `//:.bazelignore`, `crate.from_cargo` named `//:Cargo.lock` — and no phase created any of
    them. Real Bazel answers `Error in read: Unable to load package for //:requirements.lock`,
    which is a failure of the *harness* wearing the ruleset's name. This model is that missing
    contract: the adapter that emits the reference also declares where the file comes from, and
    the file is materialized in the same worker that writes the two generated files.

    **`carry_from` outranks `content`, always.** A lockfile is a resolution, and a resolution this
    harness synthesized is not the one the repo tested against; the repo's real `pnpm-lock.yaml`
    is carried into the monorepo whenever the relocation put one in the tree, and `content` is the
    floor for a repo that shipped none. Which of the two happened is visible in the file itself,
    because a synthesized one carries the generated header and a carried one does not.
    """

    path: str = Field(
        min_length=1,
        description="Monorepo-root-relative POSIX path — exactly what the label `//:<path>` (or a "
        "package-relative `srcs` entry under `<dest>/`) resolves to",
    )
    carry_from: list[str] = Field(
        default_factory=list,
        description="Worktree-relative candidates for the REAL file, in preference order. "
        "Resolved by the driver against the repo's own merged tree — never by the worker, whose "
        "worktree is one snapshot among several and would make the same file render different "
        "bytes for two repos of one wave (§11.6).",
    )
    content: str = Field(
        default="",
        description="The synthesized floor, used only when no `carry_from` candidate exists",
    )


class Resolution(FleetModel):
    """How the lockfile an adapter NAMES is *produced* when the repo ships none.

    **A lockfile is a resolution, and a resolution is the output of a resolver.** `SupportFile`
    already carries the two honest sources of one — the repo's own file (`carry_from`) and a
    synthesized floor (`content`) — and for a whole class of rulesets the floor is not a lockfile
    at all: `pip.parse` reads `requests>=2.31` and then asks its hub for `certifi`, which the
    declared spec never named, and `npm_translate_lock` over a lock with no `packages:` section
    builds an `@npm` repository containing nothing. Both failures are reported by the *ruleset*,
    about a file this harness wrote, and neither is visible until a real Bazel reads it.

    This model is the missing third source: the adapter that names the lock declares the command
    that computes its transitive closure, the files that command reads, and the path it writes.
    It is *data*, not an action — adapters stay pure (§7.5) and the driver is what executes it
    through the `CommandRunner` seam, in a scratch directory, so a test can inject a recorder and
    the offline suite stays offline.

    **Precedence is `carry_from` → `Resolution` → `content`, and never a silent fallback.** A repo
    that ships a lock has a resolution it tested against and re-resolving would change versions it
    pinned; a repo that ships none gets one computed; and a resolver that fails is a loud,
    classified per-repo failure, because an empty lock is indistinguishable from "no dependencies"
    and is exactly how this defect stayed invisible.
    """

    lock_path: str = Field(
        min_length=1,
        description="The `SupportFile.path` this resolution produces — the same string the "
        "adapter's `workspace_files()`/`package_files()` declares, so the resolver's output and "
        "the file the MODULE.bazel tag names cannot drift apart",
    )
    argv: list[str] = Field(
        min_length=1,
        description="The resolver command, argv only (never a shell string), run with the "
        "scratch directory as cwd",
    )
    inputs: list[SupportFile] = Field(
        default_factory=list,
        description="Files written into the scratch directory before the resolver runs. Each is "
        "resolved exactly like any other `SupportFile` — a `carry_from` candidate out of the "
        "repo's own tree wins over the synthesized `content`.",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables the resolver needs PINNED, OVERLAID onto the "
        "orchestrator's own environment — never a replacement for it (the driver merges; see "
        "`cli._run_resolution`). A resolver reads its toolchain selection, and sometimes its "
        "whole resolution strategy, out of the environment: `go mod download all` obeys "
        "`GOTOOLCHAIN`, and on a host where that is the default `auto` the sums written are the "
        "ones of whatever toolchain `go` decided to download, which is a fact about the host and "
        "not about this fleet. Declaring it here is what makes the produced lock a function of "
        "the adapter's own constants instead of an ambient variable no run records. Overlay and "
        "not replacement because the resolver still has to be FOUND (`PATH`) and still has to "
        "reach its caches (`HOME`); a replaced environment turns a pinned toolchain into "
        "'go: command not found'.",
    )
    timeout_s: float = Field(
        default=600.0,
        gt=0,
        description="Ceiling for the resolver process; a resolver that hangs on a stalled "
        "registry must not hold the wave open indefinitely (§11.1)",
    )

    @model_validator(mode="after")
    def _output_is_not_also_an_input(self) -> Resolution:
        clashing = sorted(f.path for f in self.inputs if f.path == self.lock_path)
        if clashing:
            raise ValueError(
                f"{self.lock_path!r} is declared both as an input and as the output of "
                f"{self.argv[0]!r}; the driver writes inputs before running the resolver, so a "
                f"pre-seeded lock would make 'resolved' and 'floor' indistinguishable"
            )
        return self


class NativeBaseline(FleetModel):
    """An `EcosystemAdapter`'s declaration of how to measure one `BuildUnit`'s PRE-migration,
    native (non-Bazel) build and test suite — the producer §12.11/D116 names and no adapter has
    ever built before (ADR-0135, `docs/DECISIONS.md`; Leg A of the native-baseline-build chain).

    **Data, not an action, for `Resolution`'s own reason above:** the adapter stays pure (§7.5) —
    no subprocess, no network, no filesystem read outside `unit.dest` — and a future worker (Leg
    B, not built by this task) is what actually runs `build_argv`/`test_argv`, through the
    `CommandRunner` seam, inside the per-ecosystem NETWORKED container ADR-0135 ruling 3 places
    architecturally outside Bazel's own `--network=none` sandbox — never inside it.

    **`test_unit_count`'s granularity is the load-bearing fact (ADR-0135 ruling 1, filed as
    `D134`).** SPEC's own comparison is `bazel query 'tests(//<dest>/...)' | wc -l` against
    `repos.baseline_test_count` — a count of MIGRATED TEST-RULE targets (`bazel/query.py::
    tests_query`), and `py.py`/`jvm.py`/`js.py` each collapse an arbitrary number of native test
    files into at most ONE such target per `BuildUnit` (`test_targets()`; `js.py` emits a second,
    non-test `ts_project` compile target alongside its `js_test`, which `bazel query 'tests(...)'`
    does not count). A native-side count of raw test CASES or test FILES is therefore a
    different unit than what it is compared against, and would false-fire
    `workers/buildverify.py::test_count_regressed` on a perfectly healthy migration the moment
    more than one native test exists. `test_unit_count` MUST therefore be computed at the SAME
    per-`BuildUnit` granularity `test_targets()` already uses on the migrated side — the number of
    that adapter's own `test_targets(unit)` entries whose `rule` is an actual Bazel TEST rule
    (`rule.endswith("_test")`, the convention every test rule macro in this fleet uses:
    `py_test`/`java_test`/`js_test`/`go_test`/`rust_test`), never a raw file or assertion count.

    **`test_unit_count` is STATIC today, not yet a measurement — see its own `Field` for the
    caveat a future Leg B/C must not skip.** Every adapter implemented so far (round VI task 105,
    Leg A) derives it from the adapter's OWN migrated-side `test_targets()`, so it is guaranteed
    to equal what the migrated `bazel query` will report and therefore cannot yet express "the
    native suite actually shrank" — wiring it unchanged into `repos.baseline_test_count` would
    make the count-regression check compare a value against itself.
    """

    build_argv: list[str] = Field(
        default_factory=list,
        description="The native build/install command, argv only (never a shell string), run "
        "with the unit's own PRE-migration repo root as cwd. Empty for a dialect with no "
        "separate build step (pytest collects and runs directly; there is nothing to compile).",
    )
    test_argv: list[str] = Field(
        min_length=1,
        description="The native test command, argv only, run with the unit's own PRE-migration "
        "repo root as cwd, after `build_argv` (if any) succeeds.",
    )
    test_unit_count: int = Field(
        ge=0,
        description="The native test suite's size at the SAME per-BuildUnit granularity "
        "test_targets() produces on the migrated side (ADR-0135 ruling 1) -- NOT a raw "
        "test-case or test-file count. CAVEAT (Leg A, round VI task 105): every adapter "
        "implemented so far derives this via native_test_unit_count(self.test_targets(unit)) "
        "-- i.e. it is a STATIC value re-deriving the MIGRATED side's own test-target count, "
        "not yet an independently observed count of the native repo's actual test suite. A "
        "future Leg B/C must NOT wire this straight through to repos.baseline_test_count "
        "unchanged: doing so makes the count-regression check (test_count_regressed, "
        "workers/buildverify.py) compare a value against itself and silently defeats the exact "
        "hazard ADR-0135 exists to catch. Leg B/C owes a REAL measurement -- parsed from "
        "actually running test_argv in a container -- before this field's value may reach the "
        "repos table.",
    )


class BuildPlan(FleetModel):
    """bazel/emit.py's output for one unit. Persisted as the buildgen worker's checkpoint;
    `artifacts/build/<run_id>/<unit>.plan.json`."""

    unit_id: RepoId | ContractId
    dest: str
    generated_by: Literal["adapter", "gazelle"] = "adapter"
    targets: list[BuildTarget] = Field(default_factory=list)
    workspace_deps: list[WorkspaceDep] = Field(default_factory=list)
    toolchains: list[ToolchainRequirement] = Field(default_factory=list)
    gazelle: GazelleConfig | None = None
    unbound_contract_kinds: list[tuple[ContractId, Ecosystem]] = Field(
        default_factory=list,
        description="Consumer ecosystem with no contract_bindings entry; §13 row 31 findings",
    )

    @model_validator(mode="after")
    def _delegation_is_explicit(self) -> BuildPlan:
        if self.generated_by == "gazelle" and self.gazelle is None:
            raise ValueError(f"{self.unit_id}: gazelle-generated plan without a GazelleConfig")
        if self.generated_by == "adapter" and not self.targets:
            raise ValueError(f"{self.unit_id}: adapter emitted no targets (Rule 11: fail loud)")
        return self
