"""`EcosystemAdapter` — the ONLY place per-language BUILD knowledge is permitted to live
(SPEC §7.5, ADR-0020).

`ManifestAdapter` (§7.3) answers *"what does this repo depend on"*; this package answers *"where
does it go and what Bazel targets describe it"*. They are separate ABCs because their cardinality
genuinely differs: `maven.py` and `gradle.py` are two manifest formats that land in **one** JVM
build story, so `ecosystems` is keyed by a **set** of `Ecosystem` members, not by one.

Three properties are load bearing, and each is the shape of a defect this file exists to prevent:

* **Purity.** Nothing here reads the DB, the filesystem, the clock, or config. An adapter is a
  pure function of `BuildUnit` + `Coordinate`, which is what makes the whole Phase 3 emission
  layer unit-testable offline and what makes §11.6's "the same plan renders the same bytes in
  every process" checkable rather than aspirational.
* **Totality.** `discover()` asserts the registry is a **total bijection** over `Ecosystem`, so
  `for_ecosystem()` is total and `layout()` (§3.3) carries no fallback branch. Adding an
  `Ecosystem` member without adding its adapter is a startup error, not a Phase 3 crash
  (§13 row 30).
* **Statelessness.** The registry holds singletons shared across the runner's whole TaskGroup
  fan-out and every `cpu_pool` child, so an instance attribute other than a `ClassVar` is a
  defect rather than a style preference: an adapter caching a parsed tree on `self` leaks repo
  A's sources into repo B. `discover()` checks it where the singleton is created (§7.2).

Everything a *caller* needs is data on the class — `monorepo_dir`, the rule names, the ruleset
and extension ids, `contract_bindings`. That is deliberate: it is what lets `bazel/layout.py`,
`bazel/generators.py` and `graph/cycles.py` stay ecosystem-free while still emitting real rules
(§12.6, §13 row 33), and it is why those callers take injected maps and Protocols rather than
importing this package.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, ClassVar, Final

from fleet.models.build import (
    BuildTarget,
    BuildUnit,
    GazelleConfig,
    Resolution,
    SupportFile,
    ToolchainRequirement,
    WorkspaceDep,
)
from fleet.models.enums import ContractKind, Ecosystem
from fleet.models.repo import Coordinate

if TYPE_CHECKING:
    from types import ModuleType

__all__ = [
    "TEST_SRC_PARTITIONED_ECOSYSTEMS",
    "AdapterCoordinateError",
    "EcosystemAdapter",
    "RegistryNotDiscoveredError",
    "adapters",
    "discover",
    "extension_bzls",
    "for_ecosystem",
    "join_segments",
    "library_loads",
    "library_rules",
    "monorepo_dirs",
    "package_relative",
    "path_segment",
    "register",
    "reset_adapters",
    "ruleset_repo_names",
    "select_entrypoint",
    "target_name",
    "union_workspace_files",
]


class AdapterCoordinateError(ValueError):
    """An adapter cannot render a coordinate it was handed into the file it declares (Rule 11).

    The ecosystem-NEUTRAL type for that failure, and it lives here for one reason: the §3.3 step 2
    driver has to contain it — one ecosystem's repos abandoned, the rest of the fleet built — and
    the only way it can do that without importing `go.py`'s (or any adapter's) own exception is
    for the adapters to raise a type this module owns. A driver that caught
    `GoModuleCoordinateError` by name would be a driver that knows an ecosystem, which is the
    thing §12.6/§13 row 33 forbid and the thing `test_no_language_directory_is_hardcoded_in_any_
    driver` greps for from the other side. Adapters subclass it where they have a language-
    specific message to add; `go.py`'s `GoModuleCoordinateError` is the first.

    **Distinct from a resolver failure, and not folded into one.** A resolution that fails is a
    fact about a package index or a version conflict — something out there did not answer. This
    is a fact about a coordinate THIS fleet's Phase 1 recorded: it names a module path or a
    version that cannot be written into the ecosystem's root file at all, so no resolver would
    ever run and no index is involved. Filing both under one finding kind would tell an operator
    to go look at a registry when the answer is in a repo's manifest.

    Raised rather than skipped: dropping the coordinate removes a dependency from the module
    graph with nothing recorded anywhere, and writing it out anyway produces a root file the
    language's own tooling rejects — naming a file this harness invented, and taking every other
    repo of that ecosystem down with it. Implementations MUST name the offending coordinate in
    the message; the driver adds the repos it was resolving for.
    """


class RegistryNotDiscoveredError(RuntimeError):
    """`for_ecosystem()` was called before `discover()` ran in this process (§7.5).

    Raised rather than lazily discovering, because §7.5 makes `discover()` a startup step run in
    *every* process — including each `cpu_pool` initializer — precisely so the total-bijection
    assertion happens once, loudly, at startup rather than on the first repo that needs an
    adapter that turns out not to exist.
    """

    def __init__(self, eco: Ecosystem) -> None:
        super().__init__(
            f"ecosystems.discover() has not run in this process, so no adapter is registered "
            f"for {eco.value!r}. §7.5: discover() is called once per process (and in every "
            f"cpu_pool initializer) so the registry's total-bijection check fails at startup "
            f"instead of mid-wave."
        )


# =======================================================================================
# Shared, ecosystem-NEUTRAL helpers. They live here rather than in each adapter so that six
# adapters sanitize a path segment the same way; the per-language part is which *parts* of a
# coordinate become segments, and that stays in the adapter.
# =======================================================================================


def path_segment(text: str) -> str:
    """One filesystem/Bazel-safe path segment: lowercased, non-`[a-z0-9._-]` folded to `_`.

    Never returns `""`: an empty segment would collapse `java/<group>/<artifact>` into
    `java/<artifact>` and silently merge two coordinates into one destination.
    """
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in text.strip().lower())
    trimmed = cleaned.strip(".")
    return trimmed or "unnamed"


def join_segments(*parts: str) -> str:
    """Sanitize every `/`-separated piece of `parts` and join the non-empty ones.

    `..` cannot survive (`.` is stripped to `""` and replaced by `unnamed`), which matters
    because the result is joined into the integration worktree by Phase 3 step 1.
    """
    segments = [
        path_segment(piece)
        for part in parts
        for piece in part.split("/")
        if piece and piece not in (".", "..")
    ]
    return "/".join(segments)


def package_relative(dest: str, paths: Iterable[str]) -> list[str]:
    """`paths` as the generated `BUILD.bazel` in `dest` must spell them: relative to `dest`.

    A `srcs` entry is resolved by Bazel **against the package that declares it**, so a
    monorepo-root-relative `py/acme-svc/client.py` inside the package `py/acme-svc` names
    `py/acme-svc/py/acme-svc/client.py` — a file that is not there. The failure is an analysis
    error naming a path nobody wrote, one directory level below the truth, and it is invisible
    to any test that compares the generator's strings with the generator's other strings.

    Idempotent on purpose: a `BuildUnit` whose `srcs` are already `dest`-relative (which is what
    the driver produces from a correctly rooted tree) passes through unchanged, so this is a
    normalization and not a second, competing convention. Sorted and deduplicated, because a
    prefix strip can collide two entries and §11.6 requires the same plan to render the same
    bytes in every process.
    """
    prefix = f"{dest.strip('/')}/" if dest.strip("/") else ""
    out: set[str] = set()
    for path in paths:
        cleaned = path.lstrip("/")
        if prefix and cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
        if cleaned:
            out.add(cleaned)
    return sorted(out)


def target_name(unit: BuildUnit) -> str:
    """The unit's primary target name: its destination's last segment, sanitized.

    Derived from `dest` rather than from the coordinate so the label reads
    `//java/com/acme/commons:commons` — the package and the target agree, and a repo whose
    `dest` was rewritten by the step-8 collision audit gets the rewritten name rather than a
    name that no longer matches where its sources landed.
    """
    leaf = unit.dest.rstrip("/").rsplit("/", maxsplit=1)[-1]
    return path_segment(leaf)


def select_entrypoint(srcs: Sequence[str], candidates: Sequence[str]) -> str | None:
    """The first `candidates` filename present in `srcs`, in the candidates' declared order.

    Ordered by the adapter's preference list, never by `srcs` order: `srcs` arrives from a
    filesystem walk, and a binary target that appears only when the walk happens to yield
    `main.py` before `__main__.py` is a target that comes and goes between runs (§11.6).
    """
    by_leaf: dict[str, list[str]] = {}
    for src in srcs:
        by_leaf.setdefault(src.rsplit("/", maxsplit=1)[-1], []).append(src)
    for candidate in candidates:
        matches = by_leaf.get(candidate)
        if matches:
            return sorted(matches)[0]
    return None


def union_workspace_files(
    units: Sequence[BuildUnit],
    per_unit: Callable[[BuildUnit], Sequence[SupportFile]],
) -> list[SupportFile]:
    """Every unit's root-file contribution as ONE list, deduped by `path`, order-independent.

    The union half of `workspace_files()`, shared by the four adapters that declare root files
    because the dedupe is ecosystem-NEUTRAL: a monorepo root holds ONE file per path whatever
    language named it, so two units of one ecosystem contributing the same path contribute one
    entry. `path` is that identity, exactly as `dest` is `_all_first_party`'s.

    The units are ordered by `(dest, unit_id)` — a total order — before the first-writer-wins
    `setdefault`, so the winner for a repeated path is a function of the fleet and not of the
    order the driver grouped the units in (§11.6). Insertion order is preserved rather than
    re-sorted by path, so a one-element sequence renders exactly the bytes, in exactly the order,
    that the adapter declared for that unit.
    """
    merged: dict[str, SupportFile] = {}
    for unit in sorted(units, key=lambda u: (u.dest, str(u.unit_id))):
        for file in per_unit(unit):
            merged.setdefault(file.path, file)
    return list(merged.values())


# =======================================================================================
# The ABC
# =======================================================================================


class EcosystemAdapter(ABC):
    """The ONLY place per-language BUILD knowledge is permitted to live (ADR-0020).

    Nothing here reads the DB, the filesystem outside `unit.dest`, or config: adapters are pure
    functions of `BuildUnit` + `Coordinate`, which is what makes them unit-testable offline.
    """

    name: ClassVar[str]
    ecosystems: ClassVar[frozenset[Ecosystem]]
    """>1 only where one build story serves several manifest formats (MAVEN + GRADLE → jvm)."""

    monorepo_dir: ClassVar[str]
    """Replaces the old hardcoded ecosystem_dir map. `layout()` is `monorepo_dir / path_tail()`
    and nothing else, so re-pointing this string moves every `dest`, `BuildTarget.package` and
    `//` label with zero edits outside this file (§13 row 33)."""

    ruleset: ClassVar[str | None] = None
    """`bazel_dep` name; the *version* is pinned by `build.ruleset_versions` (§9), never here."""

    extension: ClassVar[str | None] = None
    """`use_extension` tag id, e.g. `maven.install` — this adapter's external-dep dialect.

    The part before the `.` is BOTH the Starlark proxy variable and the **extension's own name**
    in its `.bzl`, because `render_module_bazel` emits `var = use_extension(bzl, var)`; naming it
    anything else produces `no such symbol` at module-extension evaluation."""

    extension_bzl: ClassVar[Mapping[str, str]] = {}
    """Proxy name → the `.bzl` label the ruleset **actually ships that extension in**.

    Covers every extension this adapter names, in `extension` and in `toolchain_requirements()`
    alike. It lives here rather than in `bazel/generators.py` because the label is per-ruleset
    knowledge and §13 row 6 gives `bazel/` no ecosystem exemption — and because the label a
    ruleset publishes is *not* derivable from its module name: `rules_python` ships
    `//python/extensions:pip.bzl`, `rules_rust` ships `//crate_universe:extension.bzl`
    (singular), and `go_deps` is not in `rules_go` at all but in `gazelle`. The guessed
    `@<ruleset>//:extensions.bzl` that this replaces exists in exactly one of the seven and made
    every other MODULE.bazel fail with `cannot load … no such file`. Every label here was read
    off the fetched ruleset, not inferred."""

    ruleset_repo_names: ClassVar[Mapping[str, str]] = {}
    """Module name → the APPARENT repo name that module's own labels are spelled with, for every
    ruleset whose two names differ. Absent ⇒ the apparent name IS the module name, which is the
    ordinary case and the one `bazel_dep` gives for free.

    Per-ruleset knowledge, so it lives beside `extension_bzl` for `extension_bzl`'s reason — and
    it is not derivable: a `module(name = "rules_go", repo_name = "io_bazel_rules_go")` in the
    ruleset's OWN `MODULE.bazel` governs only how that module sees itself. What a *dependent*
    sees is its own `bazel_dep(repo_name = …)`, and nothing else. `render_module_bazel` emits it
    from here, because the alternative is a monorepo whose generated `BUILD.bazel` files load
    `@io_bazel_rules_go//go:def.bzl` while its `MODULE.bazel` makes the module visible only as
    `@rules_go` — which is `No repository visible as '@io_bazel_rules_go' from main repository`
    at LOADING time, for every repo of that ecosystem at once.

    **Every label this adapter writes for the same module must agree with the entry here.** A
    `bazel_dep` has exactly one apparent name, so a `library_bzl` spelled one way and an
    `extension_bzl` spelled the other cannot both resolve; whichever one disagrees fails, and the
    extension label fails EARLIER than the rule label (`no repo visible as '@rules_go' here`,
    while Bazel is still computing the main repository mapping)."""

    src_suffixes: ClassVar[tuple[str, ...]] = ()
    """Filename suffixes this adapter's rules accept in `srcs`. Empty ⇒ every file is accepted,
    which is true only of `filegroup`.

    Per-language by nature and therefore here (§7.5): `py_library` rejects a `pyproject.toml`
    outright — *"is misplaced here (expected .py or .py3)"* — and `javac` rejects a
    `.properties`. A file the rules refuse is not dropped; `non_source_files()` routes it to the
    attribute that carries data, so a package's resources still reach the runfiles."""

    repo_name: ClassVar[str | None] = None
    """The `@repo` consumers reference. `None` for an adapter that declares no external deps."""

    library_rule: ClassVar[str]
    """The rule an ordinary buildable module gets. Declared even by a `uses_gazelle` adapter:
    §3.1 6e's coarsened `ATOMIC_WAVE` target is one library rule per `(ecosystem, scc_id)` and
    `graph/cycles.py` has no way to name it otherwise (`bazel/generators.coarse_build_targets`
    takes it as an injected map so that module stays ecosystem-free)."""

    binary_rule: ClassVar[str | None] = None
    test_rule: ClassVar[str | None] = None
    library_bzl: ClassVar[str | None] = None
    binary_bzl: ClassVar[str | None] = None
    test_bzl: ClassVar[str | None] = None
    """`load()` labels per rule kind. `None` means the rule is native and needs no load."""

    entrypoints: ClassVar[tuple[str, ...]] = ()
    """Filenames that make a unit a *binary*, in preference order (§3.3 "how a source tree maps
    to targets"). Empty ⇒ this adapter never emits a binary."""

    uses_gazelle: ClassVar[bool] = False
    """True ⇒ delegate emission, do not fake it: `generate_targets` MUST return `[]` (§12.32)."""

    degraded: ClassVar[bool] = False
    """True only for the UNKNOWN adapter. The one flag that says "this emission is the §3.1
    step 2 fallback, not a real build story" — so a `filegroup` produced because no language was
    recognized can never be mistaken, in a plan or a finding, for one an adapter chose."""

    contract_bindings: ClassVar[Mapping[ContractKind, str]] = {}
    """`ContractKind` → binding rule name, e.g. `{PROTO: "java_proto_library"}`. Declarative data
    read by `ContractAdapter` (§7.6); an absent key means "this language does not consume this
    IDL", which is a finding, not a crash (§13 row 31)."""

    version: ClassVar[int] = 1
    """Bump forces BUILD regeneration on re-run."""

    # ---------------------------------------------------------------- abstract surface

    @abstractmethod
    def path_tail(self, coordinate: Coordinate) -> str:
        """The dest path below `monorepo_dir`, e.g. `<group_path>/<name>`. Pure; deterministic.
        `layout()` (§3.3) is `monorepo_dir / path_tail(...)` and nothing else."""

    @abstractmethod
    def import_specifier(self, coordinate: Coordinate, dest: str) -> str:
        """What source code in ANOTHER unit writes to import this one after migration (§3.2 2).

        **This is the method whose absence produced `import … from '//ts/acme/lib:lib'`.** §3.2
        step 2 rewrites "import paths, package declarations, `tsconfig` path aliases, Go module
        paths, Python module paths" — every item on that list is a *language-level name*. Bazel
        labels appear one phase later, in §3.3's `BuildTarget.deps`, and they describe the same
        edge in a language no compiler reads: `tsc` answers a label with
        `error TS2307: Cannot find module '//ts/acme/lib:lib'`.

        Until this existed the only monorepo facts a `RewriteRule` could render were
        `{{dest_path}}` and `{{repo_id}}` — a path and an id — so a rule author asked "what does
        this dependency become" had nothing language-legal to answer with and composed a path
        into a label. The answer is per-language (a package name, a module path, a Java package),
        which is why it is declared here and nowhere else (§13 row 6), and why it is **abstract**:
        a new `Ecosystem` must state its own answer rather than inherit one that would be a label.

        Pure, and deliberately a function of `(coordinate, dest)` rather than of a `BuildUnit`:
        it is needed in Phase 2, where no `BuildUnit` exists yet — only the repo's published
        coordinate and its `layout()` destination.
        """

    @abstractmethod
    def workspace_deps(self, unit: BuildUnit) -> list[WorkspaceDep]:
        """Deps as MODULE.bazel must express them. THE method that `maven.install`,
        `npm_translate_lock`, `pip.parse`, `crate.from_cargo` and `go_deps.from_file` specialize.

        Takes the whole `BuildUnit` and not a bare `Sequence[Coordinate]`, which is D12's API
        gap: `unit.external_coordinates` is what most dialects need, but a ruleset that links
        **first-party** packages needs the siblings too, and a coordinate list by construction
        excludes them. `npm_translate_lock` translates ONE lockfile that carries both the
        registry packages and the `link:` entries for the workspace's own packages, so an adapter
        handed only the external half could not express half of its own dialect — which is why
        the JS half of this fleet had no way for one package to import another.
        """

    @abstractmethod
    def generate_targets(self, unit: BuildUnit) -> list[BuildTarget]:
        """Non-test BUILD targets. MUST return `[]` iff `uses_gazelle` (enforced by §12.32)."""

    @abstractmethod
    def test_targets(self, unit: BuildUnit) -> list[BuildTarget]:
        """Test targets, split out because §3.3's success criterion runs `bazel test` separately
        and a contract node legitimately has none."""

    # ---------------------------------------------------------------- defaults

    def gazelle_config(self, unit: BuildUnit) -> GazelleConfig | None:
        """Non-None iff `uses_gazelle`. Default None covers the five non-delegating adapters."""
        return None

    def toolchain_requirements(self) -> list[ToolchainRequirement]:
        """What MODULE.bazel must register for these targets to build hermetically. Default []."""
        return []

    def workspace_files(self, units: Sequence[BuildUnit]) -> list[SupportFile]:
        """The monorepo-ROOT files the tags `workspace_deps()` emits **name** (D10).

        `pip.parse(requirements_lock = "//:requirements.lock")` is a promise that a file exists at
        the monorepo root, and until this method existed nothing in the fleet kept it: real Bazel
        answered `Error in read: Unable to load package for //:requirements.lock` for every Python
        repo that had a single external dependency. Which files a ruleset's dialect needs is
        per-ruleset knowledge, so it is declared by the adapter that names them and by nothing
        else — §13 row 6 gives `bazel/` and the drivers no ecosystem exemption, and a `if
        ecosystem == NPM: write a pnpm lock` in the CLI is exactly the branch this package exists
        to prevent.

        Gated on the SAME input as `workspace_deps(unit.external_coordinates)`: a ruleset whose
        tag is not emitted names no file, and materializing a lockfile for a dialect nobody
        invoked would put an unreferenced file at the root of every monorepo.

        **These are FLEET-WIDE root files contributed by every unit of that ecosystem, never one
        repo's**, which is why this takes a `Sequence[BuildUnit]` and not one unit — the same
        widening, for the same reason, that `resolution()` already carries. Some of these files
        state a fact about the whole fleet rather than about the unit that names them
        (ADR-0048: `pnpm-workspace.yaml`'s `packages:` list must name every importer, and
        `.bazelignore` owes one `<dest>/node_modules` line per importer), and a per-unit view is
        structurally unable to see its siblings: each unit renders different bytes for one root
        path and the driver's first-writer-wins union keeps exactly one of them. Taking the units
        makes that view *expressible*; what any file contains is unchanged here.

        Deduped by `path` and order-independent — `union_workspace_files()` is the shared shape,
        because the monorepo root holds ONE file per path whatever ecosystem named it.

        Default `[]` — a dialect that names no file (`maven.install` pins per artifact) owes none.
        """
        _ = units
        return []

    def package_files(self, unit: BuildUnit) -> list[SupportFile]:
        """Files inside `unit.dest` that this adapter's own `generate_targets()` **name**.

        The `ts_project(tsconfig = ":tsconfig")` case: a label into the unit's own package, whose
        `ts_config` target has a `src` that has to be a real file. A repo that shipped a
        `tsconfig.json` already has it here — relocation carried it — so the path IS the carry,
        and `content` is the floor for a repo that shipped none.

        Default `[]`. Paths are monorepo-root-relative like every other `SupportFile`, i.e. they
        begin with `unit.dest`.
        """
        return []

    def root_targets(self, unit: BuildUnit) -> list[BuildTarget]:
        """Targets this adapter needs in the monorepo **ROOT** package (`//:`), if any.

        The `npm_link_all_packages()` case, and the reason this exists: rules_js materializes the
        pnpm virtual store in the package that owns the lockfile, so the store every
        `node_modules/<pkg>` link resolves *through* is declared by a macro call in the root
        `BUILD.bazel` and nowhere else. Without it real Bazel answers *"no such target
        '//:.aspect_rules_js/node_modules/left-pad@1.3.0'"*, from the root package, about a
        dependency the adapter spelled correctly. The links themselves are per pnpm importer
        (ADR-0048) and are emitted by `generate_targets()` into the importer's own package; only
        the root-package half belongs here.

        Root-package knowledge belongs here for the same reason `workspace_files()` does: which
        macro a ruleset requires at the workspace root is per-ruleset dialect, and §13 row 6 gives
        the drivers no ecosystem exemption. The driver unions these across the fleet and renders
        ONE root package (`bazel.generators.render_root_package`), exactly as it unions the root
        files — two adapters each writing their own root `BUILD.bazel` would drop one of them.

        Emitted with `package = ""` (the root) and gated on the same input as `workspace_deps()`:
        a repo whose ruleset tag is not emitted has no hub to link against, and the macro would
        fail on a lockfile that never mentions it. Default `[]`.
        """
        return []

    def resolution(self, units: Sequence[BuildUnit]) -> Resolution | None:
        """The resolver that COMPUTES the lockfile `workspace_files()` names, or `None`.

        **A lockfile is a resolution, and until this method existed the harness had never run a
        resolver.** `workspace_files()` offers two sources for the file a `pip.parse` or an
        `npm_translate_lock` tag names — the repo's own lock, and a floor synthesized from the
        specs Phase 1 recorded — and the floor is not a resolution: it carries no transitive
        closure. Real Bazel reports that as the ruleset's error about the harness's file
        (*"no such package '@@rules_python++pip+pypi//certifi' … referenced by
        '@@rules_python++pip+pypi_312_requests//:pkg'"*), and an `@npm` hub built from a lock with
        no `packages:` section declares no packages at all.

        Which resolver closes that gap, what its argv is and which file it writes are per-language
        facts, so they are declared here and executed by the driver: the adapter stays pure (no
        subprocess, no network, no clock), and the command reaches the host through the same
        `CommandRunner` seam every other shell-out uses.

        **The resolve is per-ECOSYSTEM over every contributing unit, never per repo**, which is
        why this takes a `Sequence[BuildUnit]` and not one unit. The lockfile it produces is ONE
        file at the monorepo root — `//:pnpm-lock.yaml`, `//:requirements.lock` — shared by every
        repo of that ecosystem in the fleet, so resolving it from a single repo's dependency set
        answers with a closure that is missing every other repo's dependencies. The driver unions
        the root files by path and first writer wins, so the second repo's resolution is silently
        dropped and real Bazel fails analysis on a package no file in the monorepo declares. A
        resolver run over the union of the units' declarations is the only input that can produce a
        lock all of them build against.

        `None` — the default — means "this adapter's dialect needs no resolution": `maven.install`
        pins per artifact and names no lockfile at all, and an adapter with no external
        coordinates in ANY of these units owes nothing either.
        """
        _ = units
        return None

    # ---------------------------------------------------------------- shared, non-overridden

    def accepts_src(self, path: str) -> bool:
        """Whether `path` may appear in this adapter's `srcs`. No suffixes declared ⇒ anything."""
        return not self.src_suffixes or path.endswith(self.src_suffixes)

    def sources(self, unit: BuildUnit) -> list[str]:
        """`unit.srcs`, package-relative and filtered to what the rules accept — the ONLY thing
        an adapter should put in a `srcs` attribute.

        Two failures in one place, deliberately: a path the package cannot resolve and a file the
        rule refuses are both "this `srcs` entry does not build", both invisible offline, and
        both per-language enough that a driver must not decide either.
        """
        return [path for path in package_relative(unit.dest, unit.srcs) if self.accepts_src(path)]

    def test_sources(self, unit: BuildUnit) -> list[str]:
        """`unit.test_srcs` under the same rule: a `py_test` rejects a fixture `.json` exactly as
        a `py_library` rejects a `pyproject.toml`."""
        return [
            path for path in package_relative(unit.dest, unit.test_srcs) if self.accepts_src(path)
        ]

    def non_source_files(self, unit: BuildUnit) -> list[str]:
        """Declared resources **plus** every `srcs` entry the rules refuse, package-relative.

        Refused files are carried, not dropped: a `pyproject.toml` a package reads at runtime is
        still an input, and silently omitting it produces a target that builds and then fails
        looking for a file the migration deleted from its runfiles. The caller decides which
        attribute carries them (`data`, `resources`), which is the part that is per-language.
        """
        refused = [
            path for path in package_relative(unit.dest, unit.srcs) if not self.accepts_src(path)
        ]
        return sorted({*package_relative(unit.dest, unit.resources), *refused})

    def external_labels(self, unit: BuildUnit) -> list[str]:
        """The `@repo//:pkg` labels for this unit's deps, deduplicated and sorted.

        Derived from this adapter's own `workspace_deps` rather than formatted independently, so
        a dep that reaches `BuildTarget.deps` and the same dep in `MODULE.bazel` cannot disagree
        about its label — a disagreement Bazel reports as an unrelated "no such package" error.
        """
        return sorted({dep.label for dep in self.workspace_deps(unit)})

    def dep_labels(self, unit: BuildUnit) -> list[str]:
        """Every label the unit's primary target depends on: internal `//` labels as the driver
        already resolved them, plus this adapter's own `@repo//` labels, deduplicated.

        The internal/external split is the *driver's* decision (`DependencyEdge.is_internal` is
        ecosystem-free, §3.3 step 2) and arrives pre-resolved in `internal_deps`; the adapter
        only knows how to spell each half in its own dialect.
        """
        external = self.external_labels(unit)
        return sorted({dep.label for dep in unit.internal_deps} | set(external))

    def __repr__(self) -> str:
        claimed = ",".join(sorted(e.value for e in getattr(self, "ecosystems", frozenset())))
        return f"<{type(self).__name__} name={getattr(self, 'name', '?')!r} eco={claimed}>"


# =======================================================================================
# Registry (§7.2's mechanism, keyed by Ecosystem instead of by name)
# =======================================================================================

_BY_ECOSYSTEM: dict[Ecosystem, EcosystemAdapter] = {}
_BY_NAME: dict[str, EcosystemAdapter] = {}
_DISCOVERED = False


def register[A: type[EcosystemAdapter]](cls: A) -> A:
    """Self-registration decorator — the §7.2 mechanism, keyed by `Ecosystem` as well as name.

    A duplicate claim is a startup error, not a silent overwrite, and the message names **both**
    claimants: with a silent overwrite the surviving adapter depends on `pkgutil` import order,
    so the same fleet would land in `ts/` on one host and `js/` on another and nothing would say
    so. Both keys are checked — the `name` because it is the human handle that reaches findings
    and logs, the `Ecosystem` because it is the dispatch key `for_ecosystem()` is total over.
    """
    name = getattr(cls, "name", None)
    if not name:
        raise RuntimeError(f"{cls.__qualname__} must declare a ClassVar `name` to register")
    claimed = getattr(cls, "ecosystems", None)
    if not claimed:
        raise RuntimeError(
            f"{cls.__qualname__} must declare a non-empty ClassVar `ecosystems`; an adapter that "
            f"claims nothing can never be reached by for_ecosystem()"
        )
    existing_named = _BY_NAME.get(name)
    if existing_named is not None:
        raise RuntimeError(
            f"duplicate EcosystemAdapter name: {name} "
            f"({type(existing_named).__module__}.{type(existing_named).__qualname__} vs "
            f"{cls.__module__}.{cls.__qualname__})"
        )
    for eco in sorted(claimed, key=lambda e: e.value):
        owner = _BY_ECOSYSTEM.get(eco)
        if owner is not None:
            raise RuntimeError(
                f"duplicate EcosystemAdapter for {eco.value}: {cls.name} "
                f"({type(owner).__module__}.{type(owner).__qualname__} vs "
                f"{cls.__module__}.{cls.__qualname__})"
            )
    inst = cls()
    _BY_NAME[name] = inst
    for eco in sorted(claimed, key=lambda e: e.value):
        _BY_ECOSYSTEM[eco] = inst
    return cls


def discover(*, force: bool = False) -> dict[Ecosystem, EcosystemAdapter]:
    """Import every module in `fleet.ecosystems`, then assert the registry is TOTAL over
    `Ecosystem` and every registered singleton is stateless.

    The bijection assertion is what makes `for_ecosystem()` total and `layout()` fallback-free
    (§1, §13 row 30): an `Ecosystem` member with no adapter fails here, at startup, rather than
    on whichever repo in wave 14 happens to be the first of that language. An import error is
    never swallowed (Rule 11) — an adapter module that cannot be imported is a broken run, not a
    missing ecosystem.

    Called in every process, including each `cpu_pool` initializer, so a pool child resolves
    adapters from its own registry rather than from a pickled one.
    """
    global _DISCOVERED
    if _DISCOVERED and not force:
        return dict(_BY_ECOSYSTEM)

    # `force` after `reset_adapters()` must re-run the decorators, and a module already in
    # `sys.modules` will not re-execute on plain import. Reloading is safe only while the
    # registry is empty, otherwise the re-run decorator trips its own duplicate check.
    reload = force and not _BY_ECOSYSTEM
    package: ModuleType = importlib.import_module("fleet.ecosystems")
    for module in pkgutil.iter_modules(package.__path__, prefix=f"{package.__name__}."):
        leaf = module.name.rsplit(".", 1)[-1]
        if module.ispkg or leaf.startswith("_") or leaf == "base":
            continue  # `contracts/` is §7.6's registry, keyed by ContractKind, not by Ecosystem
        loaded = sys.modules.get(module.name)
        if reload and loaded is not None:
            importlib.reload(loaded)
        else:
            importlib.import_module(module.name)

    missing = sorted(eco.value for eco in Ecosystem if eco not in _BY_ECOSYSTEM)
    if missing:
        raise RuntimeError(
            f"no EcosystemAdapter is registered for {missing}; §7.5 requires the registry to be "
            f"a total bijection over Ecosystem, because layout() and the §3.3 driver have no "
            f"fallback branch and an unclaimed member would abandon every repo of that language"
        )
    for inst in _BY_NAME.values():
        state = vars(inst)
        if state:
            raise RuntimeError(
                f"stateful EcosystemAdapter {inst.name!r} "
                f"({type(inst).__module__}.{type(inst).__qualname__}): instance attributes "
                f"{sorted(state)} — adapters are shared singletons and MUST be stateless; use a "
                f"ClassVar or a local"
            )
    _DISCOVERED = True
    return dict(_BY_ECOSYSTEM)


def for_ecosystem(eco: Ecosystem, /) -> EcosystemAdapter:
    """Total by construction after `discover()`. Raises only if `discover()` was never called.

    Positional-only so this module satisfies `bazel.layout.EcosystemRegistry` as written — the
    module *is* the registry object, with no wrapper class in between.
    """
    adapter = _BY_ECOSYSTEM.get(eco)
    if adapter is None:
        raise RegistryNotDiscoveredError(eco)
    return adapter


def adapters() -> list[EcosystemAdapter]:
    """Every registered adapter, once each, in `name` order — a **total** order, since `name` is
    unique by construction. Ordered so that anything derived from the registry as a *list*
    (a report, a rendered table, a digest input) is byte-identical across processes; a bare
    `_BY_ECOSYSTEM.values()` would follow import order and re-order under a different
    `PYTHONHASHSEED` (§11.6)."""
    return [_BY_NAME[name] for name in sorted(_BY_NAME)]


def reset_adapters() -> None:
    """Test hook: drop every registration so a module can be re-imported cleanly."""
    global _DISCOVERED
    _BY_ECOSYSTEM.clear()
    _BY_NAME.clear()
    _DISCOVERED = False


# =======================================================================================
# Registry-derived data, for callers that must stay ecosystem-free
# =======================================================================================


TEST_SRC_PARTITIONED_ECOSYSTEMS: Final[frozenset[Ecosystem]] = frozenset(
    {Ecosystem.PYPI, Ecosystem.MAVEN, Ecosystem.GRADLE}
)
"""D112 (round VI task 53, widened round VI task 70): the ecosystems whose discovered `srcs`
`cli._partition_test_srcs` splits into `(srcs, test_srcs)` before a `BuildUnit` exists.
`test_targets()` already reads `test_sources()` correctly for every adapter, and a per-ecosystem
test-file convention has now been decided for Python (`cli._is_python_test_src`) and JVM — both
`MAVEN` and `GRADLE`, `jvm.py`'s one adapter for two manifest formats (`cli._is_jvm_test_src`).

**`NPM` (JS) and `CARGO` (Rust) are deliberately NOT members here — both are real, measured
Bazel-analysis-time blockers in the adapter's OWN `test_targets()`, not merely an undecided
file-naming convention, and adding either without first fixing its adapter would turn a real
`bazel build`/`bazel test` GREEN today into a hard analysis failure the moment a repo's walk
matches a test-file predicate:**

* **`CARGO`**: `rust.py::test_targets()` emits `rust_test(crate = ":<lib>", srcs = test_srcs,
  ...)`, and `rules_rust`'s own `_rust_test_impl` (`rust/private/rust.bzl`, verified against the
  pinned `rules_rust@0.65.0` tag) hard-fails analysis the instant BOTH `crate` and a non-empty
  `srcs` are set: `"rust_test.crate and rust_test.srcs are mutually exclusive"`.
* **`NPM`**: `js.py::test_targets()` emits `js_test(srcs = test_srcs, deps = [f":{name}", ...],
  ...)` — but `deps` is exactly the attribute `docs/INTEGRATION_HONESTY.md`'s `## D7` entry
  already found `js_binary` (the sibling rules_js runtime rule) does NOT have
  (`generate_targets()`'s own `js_binary` was fixed to use `data` instead, and its comment says so
  in so many words). `test_targets()`'s `js_test` was written with the same `deps=` shape and has
  never been exercised — `test_srcs` has always been `()` — so this is the same defect class,
  unfixed, in a code path D7's fix never reached.

Both are genuinely unreachable in this fleet's real-Bazel history: no test in this suite has ever
built or run a generated `rust_test`/`js_test` with a non-empty `srcs`, so neither defect has ever
fired. Widening either membership here is a design decision about how to reshape that adapter's
`test_targets()` (a separate integration-test target per file for Rust; `data=` instead of
`deps=`, verified against whatever else `js_test` actually needs, for JS) — not something this
table alone can paper over. See `docs/INTEGRATION_HONESTY.md`'s `## D112` entry.

A driver-side scoping table, not adapter capability data, so it is a flat constant here rather
than an `EcosystemAdapter` `ClassVar`: §12.6/ADR-0100 forbid `cli.py` from naming `Ecosystem.PYPI`
in a bare `Compare`, so `_partition_test_srcs` reads `ecosystem not in
TEST_SRC_PARTITIONED_ECOSYSTEMS` — the compliant table-lookup shape, same runtime behavior as the
`Compare` it replaces (round VI task 62, D120)."""


def monorepo_dirs() -> dict[Ecosystem, str]:
    """`Ecosystem` → `monorepo_dir`, for callers that take the map rather than the registry.

    `graph.cycles.coarsen_atomic_scc(..., monorepo_dirs=...)` is the caller that matters: it runs
    in Phase 1, must not import Phase 3's adapter package, and its signature already takes the
    map — so the map is *derived here*, from the adapters, instead of being written out again at
    the call site where it would become a second, drifting copy of §3.3's table.
    """
    return {eco: adapter.monorepo_dir for eco, adapter in discover().items()}


def library_rules() -> dict[Ecosystem, str]:
    """`Ecosystem` → `library_rule`, for `bazel.generators.coarse_build_targets(plan, rules)`.

    §3.1 6e's coarsened target is one library rule per `(ecosystem, scc_id)`; the rule name is
    adapter knowledge (§7.5) and `bazel/` carries no ecosystem exemption (§13 row 6), so the
    driver reads it off the registry and hands it in.
    """
    return {eco: adapter.library_rule for eco, adapter in discover().items()}


def extension_bzls() -> dict[str, str]:
    """`use_extension` proxy name → its `.bzl` label, unioned over every registered adapter.

    `bazel/generators.py` renders `use_extension(...)` and must not know that `pip` lives in
    `@rules_python//python/extensions:pip.bzl`; the adapters know, and this is the same
    registry-derived-map shape `monorepo_dirs()` and `library_rules()` already use for callers
    that must stay ecosystem-free (§13 row 6).

    Two adapters claiming one proxy name with **different** labels is a startup error: the
    generator keys `use_extension` by the proxy name, so a silent last-writer-wins would emit one
    ruleset's extension under another's name and fail evaluation for a language nobody edited.
    """
    discover()
    out: dict[str, str] = {}
    for adapter in adapters():
        for var in sorted(adapter.extension_bzl):
            bzl = adapter.extension_bzl[var]
            existing = out.get(var)
            if existing is not None and existing != bzl:
                raise RuntimeError(
                    f"two adapters map the use_extension proxy {var!r} to different .bzl labels "
                    f"({existing} vs {bzl} from {adapter.name}); the proxy name is the key "
                    f"render_module_bazel emits, so one of them would be silently overwritten"
                )
            out[var] = bzl
    return out


def ruleset_repo_names() -> dict[str, str]:
    """Module name → apparent repo name, unioned over every registered adapter.

    The `extension_bzls()` shape, for the other half of the same fact: that function says which
    `.bzl` a ruleset ships an extension in, this one says what the ruleset's own labels call it.
    `bazel/generators.py` renders the `bazel_dep` and must not know that `rules_go` asks its
    dependents to spell it `@io_bazel_rules_go` (§13 row 6); the Go adapter knows, because it is
    the file that also writes `@io_bazel_rules_go//go:def.bzl` into the loads.

    Two adapters giving one module **different** apparent names is a startup error, for
    `extension_bzls()`' reason sharpened: a module has exactly ONE apparent name in the root
    module, so a last-writer-wins would silently break whichever adapter lost — and a `bazel_dep`
    is emitted once for a ruleset several adapters may share.
    """
    discover()
    out: dict[str, str] = {}
    for adapter in adapters():
        for module in sorted(adapter.ruleset_repo_names):
            repo = adapter.ruleset_repo_names[module]
            existing = out.get(module)
            if existing is not None and existing != repo:
                raise RuntimeError(
                    f"two adapters give the module {module!r} different apparent repo names "
                    f"({existing} vs {repo} from {adapter.name}); a bazel_dep has exactly one, "
                    f"so one of the two ecosystems' load labels could never resolve"
                )
            out[module] = repo
    return out


def library_loads() -> dict[Ecosystem, str]:
    """`Ecosystem` → the `.bzl` label its `library_rule` must be loaded from, where one exists.

    Absent keys are the native rules (`filegroup`), which take no `load()` — the same shape
    `coarse_build_targets(..., load_from=...)` already expects.
    """
    return {
        eco: adapter.library_bzl
        for eco, adapter in discover().items()
        if adapter.library_bzl is not None
    }
