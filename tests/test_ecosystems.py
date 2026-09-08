"""`EcosystemAdapter` registry and the six shipped adapters (SPEC §7.5, §3.3, ADR-0020).

Every test here defends one of the three properties the package exists to provide — totality,
determinism and statelessness — or the invariant that per-language knowledge stays inside it
(§12.6, §13 row 33). A test that only asserts a string equals a string is noted as such and
paired with the consequence of that string being wrong.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import ValidationError

from fleet import ecosystems
from fleet.bazel.generators import coarse_build_targets, render_build_bazel, render_module_bazel
from fleet.bazel.layout import LayoutNode, layout, scc_label
from fleet.ecosystems import go as go_adapter
from fleet.ecosystems.base import EcosystemAdapter, register, reset_adapters
from fleet.ecosystems.unknown import DEGRADED_TAG
from fleet.graph.cycles import CoarsePlan, CoarseTarget
from fleet.models.build import BuildTarget, BuildUnit, InternalDep, Resolution, WorkspaceDep
from fleet.models.enums import ContractKind, Ecosystem
from fleet.models.repo import Coordinate
from fleet.settings import BuildSection

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

ADAPTER_PACKAGES = ("src/fleet/manifests/", "src/fleet/ecosystems/")
"""§12.6's exemption list, "asserted to be exactly two, so widening it is a visible diff"."""


@pytest.fixture(autouse=True)
def registry() -> Iterator[dict[Ecosystem, ecosystems.EcosystemAdapter]]:
    """A discovered registry for every test, restored afterwards.

    Autouse and restoring because several tests below deliberately corrupt the registry (a
    shuffled import order, a stateful singleton, a re-pointed `monorepo_dir`); a corrupted
    registry leaking into the next test would turn one real failure into a cascade of unrelated
    ones and hide which assertion actually broke.
    """
    yield ecosystems.discover()
    reset_adapters()
    ecosystems.discover(force=True)


# =======================================================================================
# The registry: total, deterministic, stateless
# =======================================================================================


def test_discover_is_a_total_bijection_over_ecosystem(
    registry: dict[Ecosystem, EcosystemAdapter],
) -> None:
    """**Why:** `layout()` and the §3.3 step 2 driver carry no fallback branch — they are total
    *because the registry is* (§1, §13 row 30). An `Ecosystem` member with no adapter would not
    surface as a missing feature; it would abandon every repo of that language in whichever wave
    first contained one, days into a run."""
    assert set(registry) == set(Ecosystem)
    assert {a.name for a in ecosystems.adapters()} == {"jvm", "js", "py", "go", "rust", "unknown"}
    # MAVEN and GRADLE are two manifest formats served by ONE build story (§7.5).
    assert registry[Ecosystem.MAVEN] is registry[Ecosystem.GRADLE]


def test_discover_raises_naming_a_decoy_ecosystem_member_with_no_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**Why:** the §7.5 bijection assertion (`base.py`'s `discover()`, the `missing` branch) is
    what makes `for_ecosystem()` and `layout()` fallback-free — but no *shipped* `Ecosystem`
    member is ever missing an adapter (the previous test covers that side), so the raise branch
    itself has never fired under test. A decoy proves it: a fresh `StrEnum` carrying every real
    member's name and value plus one extra reproduces the gap without hand-editing the real,
    deliberately hand-maintained `Ecosystem` (§1) or touching the real registry, so the only way
    `discover()` can fail here is the one member the real adapters never claimed."""
    from enum import StrEnum

    from fleet.ecosystems import base

    decoy = StrEnum(
        "Ecosystem",
        [(member.name, member.value) for member in Ecosystem] + [("DECOY", "decoy")],
    )
    monkeypatch.setattr(base, "Ecosystem", decoy)
    reset_adapters()
    with pytest.raises(RuntimeError) as excinfo:
        ecosystems.discover(force=True)
    message = str(excinfo.value)
    assert "no EcosystemAdapter is registered for ['decoy']" in message
    assert "total bijection over Ecosystem" in message
    # `monkeypatch`'s automatic revert of `base.Ecosystem` happens in its own fixture
    # finalizer, which — per pytest's LIFO teardown order — runs AFTER the autouse
    # `registry` fixture's teardown (registry was set up after monkeypatch, since it
    # depends on it transitively via fixture ordering). `registry`'s teardown calls
    # `ecosystems.discover(force=True)` to restore a clean registry for the next test;
    # if `base.Ecosystem` is still the decoy enum at that point, that call correctly
    # (and spuriously, from this test's perspective) raises the same RuntimeError again,
    # surfacing as a teardown ERROR on this test. Force the revert here, before
    # `registry`'s teardown runs, so the shared fixture sees the real `Ecosystem` again.
    monkeypatch.undo()


def test_duplicate_ecosystem_registration_raises_naming_both_claimants() -> None:
    """**Why:** a silent overwrite makes the surviving adapter a function of `pkgutil` import
    order, so the same fleet lands in one directory on one host and another elsewhere, with
    nothing in the logs saying so. The message must name both classes or the operator cannot
    tell which file to delete."""

    class Intruder(EcosystemAdapter):
        name: ClassVar[str] = "intruder"
        ecosystems: ClassVar[frozenset[Ecosystem]] = frozenset({Ecosystem.NPM})
        monorepo_dir: ClassVar[str] = "elsewhere"
        library_rule: ClassVar[str] = "ts_project"

        def path_tail(self, coordinate: Coordinate) -> str:
            return coordinate.name

        def import_specifier(self, coordinate: Coordinate, dest: str) -> str:
            return coordinate.name

        def workspace_deps(self, unit: BuildUnit) -> list[WorkspaceDep]:
            return []

        def generate_targets(self, unit: BuildUnit) -> list[BuildTarget]:
            return []

        def test_targets(self, unit: BuildUnit) -> list[BuildTarget]:
            return []

    with pytest.raises(RuntimeError) as excinfo:
        register(Intruder)
    message = str(excinfo.value)
    assert "npm" in message
    assert "Intruder" in message
    assert "JsAdapter" in message


def test_duplicate_name_registration_raises_before_the_ecosystem_check() -> None:
    """**Why:** `name` is the handle that reaches findings, logs and `BuildPlan` provenance. Two
    adapters sharing it makes every one of those rows ambiguous even when they claim disjoint
    ecosystems and the dispatch dict never notices."""

    class Twin(EcosystemAdapter):
        name: ClassVar[str] = "js"  # already taken by ecosystems/js.py
        ecosystems: ClassVar[frozenset[Ecosystem]] = frozenset({Ecosystem.UNKNOWN})
        monorepo_dir: ClassVar[str] = "elsewhere"
        library_rule: ClassVar[str] = "filegroup"

        def path_tail(self, coordinate: Coordinate) -> str:
            return coordinate.name

        def import_specifier(self, coordinate: Coordinate, dest: str) -> str:
            return coordinate.name

        def workspace_deps(self, unit: BuildUnit) -> list[WorkspaceDep]:
            return []

        def generate_targets(self, unit: BuildUnit) -> list[BuildTarget]:
            return []

        def test_targets(self, unit: BuildUnit) -> list[BuildTarget]:
            return []

    with pytest.raises(RuntimeError, match=r"duplicate EcosystemAdapter name: js"):
        register(Twin)


def test_the_registry_is_identical_under_a_shuffled_import_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**Why:** `pkgutil.iter_modules` order is a filesystem fact, not a contract. If any output
    depended on it, the same repo would get a different `dest` on a different host and §12.21's
    run-equivalence digest — which compares two runs of the same fleet — would be comparing two
    orderings rather than two results, i.e. would mean nothing."""
    import pkgutil

    before = {eco: adapter.name for eco, adapter in ecosystems.discover().items()}
    before_order = [a.name for a in ecosystems.adapters()]
    real_iter = pkgutil.iter_modules

    def reversed_iter(path: object = None, prefix: str = "") -> object:
        return reversed(list(real_iter(path, prefix)))  # type: ignore[arg-type]

    monkeypatch.setattr(pkgutil, "iter_modules", reversed_iter)
    reset_adapters()
    after = {eco: adapter.name for eco, adapter in ecosystems.discover(force=True).items()}

    assert after == before
    assert [a.name for a in ecosystems.adapters()] == sorted(before_order)


def test_every_adapter_is_stateless_after_discovery(
    registry: dict[Ecosystem, EcosystemAdapter],
) -> None:
    """**Why (§7.2):** the registry holds singletons shared across the runner's whole TaskGroup
    fan-out and every `cpu_pool` child. An adapter caching a parsed tree on `self` leaks repo A's
    sources into repo B's BUILD file — a wrong-but-green build that no later phase can detect."""
    for adapter in ecosystems.adapters():
        assert vars(adapter) == {}, f"{adapter.name} carries instance state"


def test_discovery_rejects_a_stateful_singleton(
    registry: dict[Ecosystem, EcosystemAdapter],
) -> None:
    """**Why:** the statelessness rule is only worth stating if it is *checked* — the previous
    test passes vacuously the day someone adds `self._cache = {}` in a method rather than in
    `__init__`. This proves the check has teeth by making a live singleton stateful."""
    adapter = ecosystems.for_ecosystem(Ecosystem.NPM)
    adapter.leaked = {"repo-a": ["src/a.ts"]}  # type: ignore[attr-defined]
    try:
        with pytest.raises(RuntimeError, match=r"stateful EcosystemAdapter 'js'.*leaked"):
            ecosystems.discover(force=True)
    finally:
        del adapter.leaked  # type: ignore[attr-defined]


def test_for_ecosystem_before_discovery_fails_loud() -> None:
    """**Why (Rule 11):** §7.5 makes `discover()` a startup step in every process precisely so
    the bijection check fires once, loudly. A lazily-discovering `for_ecosystem` would move that
    check into the first repo that needs it — inside a wave, inside a lease, mid-run."""
    reset_adapters()
    with pytest.raises(ecosystems.RegistryNotDiscoveredError, match=r"discover\(\) has not run"):
        ecosystems.for_ecosystem(Ecosystem.MAVEN)


# =======================================================================================
# One test per ecosystem: a representative tree → its destination and its rules
# =======================================================================================


def _dest_of(label: str) -> str:
    """`//ts/acme/tokens:tokens` → `ts/acme/tokens` — the package half of an internal label."""
    return label.removeprefix("//").partition(":")[0]


def _unit(
    unit_id: str,
    ecosystem: Ecosystem,
    dest: str,
    *,
    srcs: Sequence[str] = (),
    test_srcs: Sequence[str] = (),
    resources: Sequence[str] = (),
    published: Coordinate | None = None,
    internal_deps: Sequence[InternalDep | str] = (),
    external: Sequence[Coordinate] = (),
) -> BuildUnit:
    return BuildUnit(
        unit_id=unit_id,
        ecosystem=ecosystem,
        dest=dest,
        srcs=list(srcs),
        test_srcs=list(test_srcs),
        resources=list(resources),
        published=published,
        internal_deps=[
            dep if isinstance(dep, InternalDep) else InternalDep(label=dep, dest=_dest_of(dep))
            for dep in internal_deps
        ],
        external_coordinates=list(external),
    )


def test_jvm_maps_a_maven_module_to_java_targets() -> None:
    """A real Maven module: Standard Directory Layout, a resource bundle, a `Main`, JUnit tests,
    one internal dep and one external artifact.

    **Why:** `main_class` is derived from the path *below* the source root, so a wrong derivation
    produces a `java_binary` that builds green and dies on `bazel run` with `ClassNotFound` —
    a failure that surfaces in production, not in Phase 3's exit code.
    """
    adapter = ecosystems.for_ecosystem(Ecosystem.MAVEN)
    coordinate = Coordinate(ecosystem=Ecosystem.MAVEN, group="com.acme", name="commons")
    assert adapter.monorepo_dir == "java"
    assert adapter.path_tail(coordinate) == "com/acme/commons"

    unit = _unit(
        "acme-commons",
        Ecosystem.MAVEN,
        "java/com/acme/commons",
        srcs=[
            "src/main/java/com/acme/commons/Main.java",
            "src/main/java/com/acme/commons/Widget.java",
            "src/main/java/com/acme/commons/internal/Cache.java",
        ],
        test_srcs=["src/test/java/com/acme/commons/WidgetTest.java"],
        resources=["src/main/resources/messages.properties"],
        published=coordinate,
        internal_deps=["//java/com/acme/core:core"],
        external=[
            Coordinate(ecosystem=Ecosystem.MAVEN, group="com.google.guava", name="guava",
                       version_spec="33.3.1-jre")
        ],
    )
    targets = adapter.generate_targets(unit)
    library, binary = targets[0], targets[1]
    assert (library.rule, library.name) == ("java_library", "commons")
    assert library.label == "//java/com/acme/commons:commons"
    assert library.attrs["resources"] == ["src/main/resources/messages.properties"]
    assert library.deps == ["//java/com/acme/core:core", "@maven//:guava"]
    assert (binary.rule, binary.attrs["main_class"]) == ("java_binary", "com.acme.commons.Main")

    tests = adapter.test_targets(unit)
    assert [(t.rule, t.name, t.testonly) for t in tests] == [
        ("java_test", "commons_test", True)
    ]
    assert tests[0].deps[0] == ":commons"

    (dep,) = adapter.workspace_deps(unit)
    assert (dep.ruleset, dep.extension, dep.repo_name) == (
        "rules_jvm_external", "maven.install", "maven",
    )
    assert dep.attrs == {}, "maven versions come from MVS (§3.3 step 3), never from the adapter"
    assert adapter.contract_bindings[ContractKind.PROTO] == "java_proto_library"


def test_gradle_resolves_to_the_same_adapter_as_maven() -> None:
    """**Why (§7.5):** Gradle and Maven are two manifest formats with one build story. Two
    adapters would mean two copies of the same rule names, and the copies drift — a `gradle` repo
    and a `maven` repo landing in different directories for no reason a reviewer can name."""
    assert ecosystems.for_ecosystem(Ecosystem.GRADLE).monorepo_dir == "java"
    coordinate = Coordinate(ecosystem=Ecosystem.GRADLE, group="com.acme", name="tooling")
    assert ecosystems.for_ecosystem(Ecosystem.GRADLE).path_tail(coordinate) == "com/acme/tooling"


def test_js_maps_a_scoped_npm_package_to_ts_targets() -> None:
    """A real scoped TypeScript package: `src/**`, a spec suite, an entry point, one workspace
    dep and one registry dep.

    **Why:** the scope must stay a *directory* (`ts/acme/ui`), or `@acme/ui` and `@corp/ui` both
    claim `ts/ui` and Phase 1's collision audit reports a contest that only exists because this
    function flattened it. And `declaration = True` is what lets consumers type-check against
    this library at all.
    """
    adapter = ecosystems.for_ecosystem(Ecosystem.NPM)
    coordinate = Coordinate(ecosystem=Ecosystem.NPM, group="@acme", name="ui")
    assert adapter.monorepo_dir == "ts"
    assert adapter.path_tail(coordinate) == "acme/ui"
    assert adapter.path_tail(Coordinate(ecosystem=Ecosystem.NPM, name="react")) == "react"

    unit = _unit(
        "acme-ui",
        Ecosystem.NPM,
        "ts/acme/ui",
        srcs=["src/index.ts", "src/Button.tsx", "src/theme/tokens.ts"],
        test_srcs=["src/Button.spec.ts"],
        published=coordinate,
        internal_deps=["//ts/acme/tokens:tokens"],
        external=[Coordinate(ecosystem=Ecosystem.NPM, group="", name="react",
                             version_spec="^18.3.1")],
    )
    library, tsconfig, links, binary = adapter.generate_targets(unit)
    assert (library.rule, library.name) == ("ts_project", "ui")
    assert library.attrs["declaration"] is True
    assert library.deps == ["//ts/acme/tokens:tokens", "//ts/acme/ui:node_modules/react"]
    assert (binary.rule, binary.attrs["entry_point"]) == ("js_binary", "src/index.js")

    # ADR-0048's importer half, emitted beside the labels that need it: with one pnpm importer
    # per JS repo, `npm_link_all_packages()` is called in THIS package and the links it creates
    # are `//ts/acme/ui:node_modules/<pkg>`. Without the call every one of those deps is "no such
    # target", and rules_js `fail()`s the macro outright in a package that is neither the pnpm
    # root nor a workspace importer — so the call, the labels and the importer list are one
    # decision the adapter owes in full.
    assert (links.rule, links.name, links.package) == (
        "npm_link_all_packages",
        "node_modules",
        "ts/acme/ui",
    )
    assert links.load_from == "@npm//:defs.bzl"
    assert links.visibility == [], "the macro takes no visibility attribute; passing one is a load"

    # D7: `js_binary` has no `deps` attribute at all — a `deps` here is not a redundant edge, it
    # is `//ts/acme/ui:ui_bin: no such attribute 'deps' in 'js_binary' rule` and the package does
    # not load. The edge itself is real, so it is MOVED rather than deleted: `data` carries the
    # `ts_project`, whose own `deps` (the internal label and the `//ts/acme/ui:node_modules/react`
    # link) reach the binary's runfiles through it. A binary that lost the edge would still
    # analyse and would die at `bazel run` with MODULE_NOT_FOUND, the worse of the two failures.
    assert binary.deps == []
    assert binary.attrs["data"] == [f":{library.name}"]
    assert library.deps == ["//ts/acme/tokens:tokens", "//ts/acme/ui:node_modules/react"]

    (test,) = adapter.test_targets(unit)
    assert (test.rule, test.name, test.testonly) == ("js_test", "ui_test", True)

    # D10: `tsconfig = ":tsconfig"` on the library is a label into this package, and the target it
    # names is emitted beside it. Before, it named nothing and every generated TS package failed
    # to load — an adapter that references a label owes the target, in the same method.
    assert (tsconfig.rule, tsconfig.name) == ("ts_config", "tsconfig")
    assert library.attrs["tsconfig"] == f":{tsconfig.name}"
    assert tsconfig.attrs["src"] == "tsconfig.json"

    (dep,) = adapter.workspace_deps(unit)
    assert dep.extension == "npm.npm_translate_lock"
    assert dep.attrs["pnpm_lock"] == "//:pnpm-lock.yaml"
    assert ContractKind.AVRO not in adapter.contract_bindings, "§13 row 31: a finding, not a lie"

    # `//ts/acme/ui:node_modules/react`, not `@npm//:react`. rules_js exposes an npm package as a
    # LINK that `npm_link_all_packages()` declares in an importer's package; the hub's own root
    # package declares `noop`, `sync` and nothing else, so the maven-shaped default resolved to
    # nothing at all ("target 'react' not declared in package ''"). The ROOT call survives the
    # move to one importer per repo for its own reason: rules_js emits the pnpm virtual store
    # every link resolves through under `if is_root:`, so the root macro is what makes the store
    # exist even though no label points into the root package any more.
    (store,) = adapter.root_targets(unit)
    assert (store.package, store.name, store.rule) == ("", "node_modules", "npm_link_all_packages")
    assert store.load_from == "@npm//:defs.bzl"
    assert store.visibility == [], "the macro takes no visibility attribute; passing one is a load"
    assert adapter.external_labels(unit) == [f"//{unit.dest}:{links.name}/react"]


def test_a_first_party_sibling_is_linked_as_an_npm_package_not_only_as_a_label() -> None:
    """The JS half of D12: how one package in this monorepo imports another.

    **Why, verbatim.** `ts_project(deps = ["//ts/acme/tokens:tokens"])` puts the sibling's
    declarations in the action and gives `tsc` no way to *resolve a specifier* to them, so
    `import … from '@acme/tokens'` failed with `error TS2307: Cannot find module '@acme/tokens'`
    on a dependency that was present, built and correct. rules_js resolves a first-party package
    the way pnpm does — a `link:` entry in the lockfile, materialized as a `node_modules` link by
    `npm_link_all_packages()` — and every part of that chain is asserted here because each part
    fails in a way that names something else:

    * the `link:` in the resolver's manifest, or pnpm resolves nothing and the lock has no
      first-party entry;
    * the sibling's own `package.json` as a resolver input, because pnpm reads the manifest AT
      the linked directory to learn the package's name — without it the resolver exits non-zero;
    * the `//<dest>:node_modules/@acme/tokens` label on the consumer, because that is the only
      name `tsc` can resolve the specifier through;
    * and `npm_package(name = "pkg")` on the *dependency*, which is the target
      `npm_translate_lock` generates the link against (`npm_package_target_name` defaults to
      `"pkg"`). Linking the `ts_project` directly is the subtle one: it stages the declarations
      and NOT the package's `package.json`, so the failure appears only inside a sandbox and
      reads as the consumer's bug.
    """
    adapter = ecosystems.for_ecosystem(Ecosystem.NPM)
    sibling = InternalDep(
        label="//ts/acme/tokens:tokens",
        dest="ts/acme/tokens",
        published=Coordinate(ecosystem=Ecosystem.NPM, group="@acme", name="tokens"),
    )
    unit = _unit(
        "acme-ui",
        Ecosystem.NPM,
        "ts/acme/ui",
        srcs=["ts/acme/ui/src/index.ts", "ts/acme/ui/package.json"],
        published=Coordinate(ecosystem=Ecosystem.NPM, group="@acme", name="ui"),
        internal_deps=[sibling],
        external=[Coordinate(ecosystem=Ecosystem.NPM, name="react", version_spec="^18.0.0")],
    )

    # 1. the sibling is a row in `workspace_deps` — which is what handing it the BuildUnit bought.
    assert {dep.coordinate.name for dep in adapter.workspace_deps(unit)} == {"react", "tokens"}
    # 2. ... so the consumer depends on the LINK, beside the plain label the driver resolved.
    labels = adapter.dep_labels(unit)
    assert "//ts/acme/ui:node_modules/@acme/tokens" in labels, labels
    assert "//ts/acme/tokens:tokens" in labels, labels

    # 3. the resolver is asked for a `link:`, never for a registry version of a sibling — in
    #    THIS repo's own importer manifest (ADR-0048), and importer-relative, because pnpm
    #    resolves `link:` against the directory of the manifest that declares it.
    plan = adapter.resolution([unit])
    assert plan is not None
    inputs = {support.path: support for support in plan.inputs}
    assert "dependencies" not in json.loads(inputs["package.json"].content)
    manifest = json.loads(inputs["ts/acme/ui/package.json"].content)
    assert manifest["dependencies"] == {
        "react": "^18.0.0",
        "@acme/tokens": "link:../tokens",
    }, manifest
    # 4. ... and the sibling's own manifest travels with it, carried from the merged tree.
    sibling_manifest = inputs["ts/acme/tokens/package.json"]
    assert sibling_manifest.carry_from == ["ts/acme/tokens/package.json"]
    assert json.loads(sibling_manifest.content)["name"] == "@acme/tokens"

    # 5. the dependency side: the target the generated link resolves against.
    (pkg,) = [t for t in adapter.generate_targets(unit) if t.rule == "npm_package"]
    assert (pkg.name, pkg.package) == ("pkg", "ts/acme/ui")
    assert pkg.srcs == [":ui", "package.json"], (
        "the package.json must be in the npm_package, or the linked directory has declarations "
        "and no manifest and tsc cannot resolve the specifier"
    )


def test_a_js_unit_with_no_npm_surface_at_all_emits_no_hub_reference() -> None:
    """**Why:** every reference to `@npm` — the `npm_translate_lock` tag, the root lockfiles, the
    `npm_link_all_packages()` macro and the resolver — is gated on the SAME question, and a unit
    that emits the macro but no tag fails at load with `no such repository '@npm'` while a unit
    that emits the tag but no lockfile fails inside the ruleset. A sibling with no published
    coordinate cannot be linked (there is no registry name to link it under), so it must not
    switch the hub on either."""
    adapter = ecosystems.for_ecosystem(Ecosystem.NPM)
    unit = _unit(
        "acme-solo",
        Ecosystem.NPM,
        "ts/acme/solo",
        srcs=["ts/acme/solo/src/index.ts"],
        published=Coordinate(ecosystem=Ecosystem.NPM, group="@acme", name="solo"),
        internal_deps=["//ts/acme/anon:anon"],  # coerced with published=None: not linkable
    )
    assert adapter.workspace_deps(unit) == []
    assert adapter.workspace_files([unit]) == []
    assert adapter.root_targets(unit) == []
    assert adapter.resolution([unit]) is None
    assert adapter.dep_labels(unit) == ["//ts/acme/anon:anon"]


def test_py_maps_a_distribution_to_python_targets() -> None:
    """A real distribution: a package directory, a `__main__.py`, a `tests/` suite, a data file.

    **Why:** PEP 503 says `Flask_SQLAlchemy` and `flask-sqlalchemy` are the same distribution.
    Two directories for one distribution is a collision the audit would report as two repos
    contesting a name — a contest invented here, with an ownership ladder run to resolve it.
    """
    adapter = ecosystems.for_ecosystem(Ecosystem.PYPI)
    assert adapter.monorepo_dir == "py"
    assert adapter.path_tail(Coordinate(ecosystem=Ecosystem.PYPI, name="Flask_SQLAlchemy")) == (
        "flask-sqlalchemy"
    )

    unit = _unit(
        "acme-svc",
        Ecosystem.PYPI,
        "py/acme-svc",
        srcs=["acme_svc/__init__.py", "acme_svc/__main__.py", "acme_svc/client.py"],
        test_srcs=["tests/test_client.py", "tests/conftest.py"],
        resources=["acme_svc/schema.json"],
        published=Coordinate(ecosystem=Ecosystem.PYPI, name="acme-svc"),
        internal_deps=["//py/acme-core:acme-core"],
        external=[Coordinate(ecosystem=Ecosystem.PYPI, name="httpx", version_spec=">=0.27")],
    )
    library, binary = adapter.generate_targets(unit)
    assert (library.rule, library.name) == ("py_library", "acme-svc")
    assert library.attrs["imports"] == ["."]
    assert library.attrs["data"] == ["acme_svc/schema.json"]
    # `@pypi//httpx`, not `@pypi//:httpx`: a `pip.parse` hub gives every distribution its own
    # package, and real Bazel reports the base class's `@<repo>//:<name>` spelling as
    # "target 'httpx' not declared in package ''; however, a source directory of this name
    # exists". The maven-shaped default is wrong for this ruleset, so `PyAdapter` overrides it.
    assert library.deps == ["//py/acme-core:acme-core", "@pypi//httpx"]
    assert (binary.rule, binary.attrs["main"]) == ("py_binary", "acme_svc/__main__.py")

    (test,) = adapter.test_targets(unit)
    assert (test.rule, test.name, test.attrs["main"]) == (
        "py_test", "acme-svc_test", "tests/conftest.py",
    )

    (dep,) = adapter.workspace_deps(unit)
    assert (dep.extension, dep.repo_name) == ("pip.parse", "pypi")
    (toolchain,) = adapter.toolchain_requirements()
    assert (toolchain.ruleset, toolchain.extension) == ("rules_python", "python.toolchain")


def test_go_delegates_to_gazelle_and_emits_no_targets() -> None:
    """A real Go module: `cmd/`, an internal package, `*_test.go`.

    **Why (§3.3, §12.32):** `uses_gazelle` exists because delegation is a shipped case, not a
    hypothesis. An adapter that emitted a half-inferred `go_library` *and* let Gazelle run would
    make the generated tree depend on which of the two wrote last; `generate_targets() == []` is
    the mechanical form of "it does not pretend". `# gazelle:prefix` is the load-bearing
    directive: without it every intra-module import resolves as an unknown external dependency.
    """
    adapter = ecosystems.for_ecosystem(Ecosystem.GO)
    assert adapter.uses_gazelle is True
    coordinate = Coordinate(ecosystem=Ecosystem.GO, group="github.com/acme", name="commons")
    assert adapter.monorepo_dir == "go"
    assert adapter.path_tail(coordinate) == "commons"

    unit = _unit(
        "acme-commons-go",
        Ecosystem.GO,
        "go/commons",
        srcs=["cmd/serve/main.go", "internal/store/store.go", "commons.go"],
        test_srcs=["internal/store/store_test.go"],
        published=coordinate,
        external=[Coordinate(ecosystem=Ecosystem.GO, group="github.com/stretchr",
                             name="testify", version_spec="v1.9.0")],
    )
    assert adapter.generate_targets(unit) == []
    assert adapter.test_targets(unit) == []

    config = adapter.gazelle_config(unit)
    assert config is not None
    assert config.prefix == "github.com/acme/commons"
    assert "# gazelle:prefix github.com/acme/commons" in config.directives
    assert "vendor" in config.exclude

    (dep,) = adapter.workspace_deps(unit)
    assert dep.extension == "go_deps.from_file"
    assert dep.repo_name == "com_github_stretchr_testify", "Gazelle's own repo naming"
    assert adapter.library_rule == "go_library", "still needed for §3.1 6e's coarsened target"


def test_rust_maps_a_crate_to_rust_targets() -> None:
    """A real crate: `src/lib.rs` plus `src/main.rs`, modules, an integration test.

    **Why:** `crate_root` must be explicit. With both files present and no root attribute,
    `rules_rust` picks one by convention and compiles the binary's `main` into the library —
    a link error whose message names neither file. And the unit-test `rust_test` takes `crate =`,
    not `srcs =`: Rust unit tests live *inside* the crate, so a separate compilation reports green
    while running none of them.

    **D112 (round VI task 86):** `tests/roundtrip.rs` — a real Cargo integration-test file — must
    land on its OWN `rust_test(srcs=[...])` target, never combined with the `crate=` unit-test
    target: `rules_rust`'s `_rust_test_impl` hard-fails Bazel analysis the instant `crate` and a
    non-empty `srcs` are both set on the same target, so this is the actual discriminator between
    the pre-fix shape (one combined target) and the post-fix shape (two separate targets) — a
    single `(test,) = adapter.test_targets(unit)` unpack would have stayed green under either.
    """
    adapter = ecosystems.for_ecosystem(Ecosystem.CARGO)
    assert adapter.monorepo_dir == "rust"
    assert adapter.path_tail(Coordinate(ecosystem=Ecosystem.CARGO, name="acme-store")) == (
        "acme-store"
    )

    unit = _unit(
        "acme-store",
        Ecosystem.CARGO,
        "rust/acme-store",
        srcs=["src/lib.rs", "src/main.rs", "src/index/mod.rs"],
        test_srcs=["tests/roundtrip.rs"],
        published=Coordinate(ecosystem=Ecosystem.CARGO, name="acme-store"),
        internal_deps=["//rust/acme-core:acme-core"],
        external=[Coordinate(ecosystem=Ecosystem.CARGO, name="serde", version_spec="1.0")],
    )
    library, binary = adapter.generate_targets(unit)
    assert (library.rule, library.name) == ("rust_library", "acme-store")
    assert library.attrs["crate_name"] == "acme_store", "rustc identifiers cannot carry '-'"
    assert library.attrs["crate_root"] == "src/lib.rs"
    assert library.deps == ["//rust/acme-core:acme-core", "@crates//:serde"]
    assert (binary.rule, binary.attrs["crate_root"]) == ("rust_binary", "src/main.rs")

    unit_test, integration_test = adapter.test_targets(unit)
    assert (unit_test.rule, unit_test.name) == ("rust_test", "acme-store_test")
    assert unit_test.attrs["crate"] == ":acme-store"
    assert unit_test.srcs == [], "crate= and a non-empty srcs= are mutually exclusive to Bazel"

    assert (integration_test.rule, integration_test.name) == (
        "rust_test",
        "acme-store_roundtrip_test",
    )
    assert integration_test.srcs == ["tests/roundtrip.rs"]
    assert "crate" not in integration_test.attrs, (
        "crate= alongside a non-empty srcs= is the exact rules_rust@0.65.0 analysis-time failure "
        "D112's Rust slice exists to avoid"
    )
    assert integration_test.deps == [":acme-store", "//rust/acme-core:acme-core", "@crates//:serde"]

    (dep,) = adapter.workspace_deps(unit)
    assert dep.attrs["cargo_lockfile"] == "//:Cargo.lock"


def test_rust_test_targets_emits_one_per_file_never_combining_crate_with_srcs() -> None:
    """`test_targets()` never puts a nonempty `srcs` on the SAME target as `crate=` — the exact
    shape `rules_rust@0.65.0`'s `_rust_test_impl` hard-fails Bazel analysis over — however many
    integration-test files `test_sources(unit)` hands it (here: two, so the discriminator is
    "N files in ⇒ N separate targets out", not just "at least one target has no `crate`").

    `cli._is_rust_test_src`'s own DIRECT-children-of-`tests/` restriction (a `tests/common/mod.rs`
    helper module is not its own Cargo test binary) is exercised at the `cli._partition_test_srcs`
    layer, which is where it runs in production (`BuildUnit.test_srcs` arrives here already
    partitioned) — this adapter-level test constructs `unit.test_srcs` directly, so it is the
    wrong layer to re-assert that restriction; see `tests/test_build_e2e.py`'s Rust D112 fixture
    for the end-to-end proof that a nested helper file gets no `rust_test` target of its own.
    """
    adapter = ecosystems.for_ecosystem(Ecosystem.CARGO)
    unit = _unit(
        "acme-store",
        Ecosystem.CARGO,
        "rust/acme-store",
        srcs=["src/lib.rs"],
        test_srcs=["tests/roundtrip.rs", "tests/widgets.rs"],
    )
    targets = adapter.test_targets(unit)
    assert len(targets) == 3, targets  # one crate= unit-test target + one per integration file
    for target in targets:
        assert not (target.attrs.get("crate") and target.srcs), (
            "crate= combined with a non-empty srcs= on one target",
            target,
        )
    integration = {t.name: t for t in targets if t.srcs}
    assert set(integration) == {"acme-store_roundtrip_test", "acme-store_widgets_test"}, targets
    assert integration["acme-store_roundtrip_test"].srcs == ["tests/roundtrip.rs"]
    assert integration["acme-store_widgets_test"].srcs == ["tests/widgets.rs"]


def test_unknown_falls_back_visibly_instead_of_raising() -> None:
    """A repo with no recognizable manifest: a shell script, a Dockerfile, a README.

    **Why (§3.1 step 2):** the fallback must exist — `layout()` is total *because* UNKNOWN is a
    registered value rather than a null check — and it must be unmistakable. A `filegroup`
    emitted because no language was recognized that looked like an adapter's considered choice
    would let a fleet report 250 migrated repos of which 40 were never compiled at all.
    """
    adapter = ecosystems.for_ecosystem(Ecosystem.UNKNOWN)
    assert adapter.degraded is True
    assert all(a.degraded is False for a in ecosystems.adapters() if a.name != "unknown")
    assert adapter.monorepo_dir == "misc"
    assert adapter.path_tail(Coordinate(ecosystem=Ecosystem.UNKNOWN, name="ops-scripts")) == (
        "ops-scripts"
    )

    unit = _unit(
        "ops-scripts",
        Ecosystem.UNKNOWN,
        "misc/ops-scripts",
        srcs=["deploy.sh", "Dockerfile", "README.md"],
        test_srcs=["smoke.sh"],
        external=[Coordinate(ecosystem=Ecosystem.UNKNOWN, name="mystery")],
    )
    (target,) = adapter.generate_targets(unit)
    assert (target.rule, target.name) == ("filegroup", "ops-scripts")
    assert target.attrs["tags"] == [DEGRADED_TAG]
    assert target.srcs == ["Dockerfile", "README.md", "deploy.sh", "smoke.sh"]
    assert adapter.test_targets(unit) == []
    assert adapter.workspace_deps(unit) == []
    assert adapter.contract_bindings == {}


@pytest.mark.parametrize("eco", sorted(Ecosystem, key=lambda e: e.value))
def test_every_adapter_declares_a_gazelle_config_iff_it_delegates(eco: Ecosystem) -> None:
    """**Why (§12.32):** `uses_gazelle` has exactly two consistent shapes — delegate and emit
    nothing, or emit targets and no config. Any third combination makes `bazel/emit.py`'s
    two-branch driver write a BUILD file that something else then overwrites."""
    adapter = ecosystems.for_ecosystem(eco)
    unit = _unit("u", eco, f"{adapter.monorepo_dir}/u", srcs=["a.txt"])
    if adapter.uses_gazelle:
        assert adapter.generate_targets(unit) == []
        assert adapter.gazelle_config(unit) is not None
    else:
        assert adapter.generate_targets(unit) != []
        assert adapter.gazelle_config(unit) is None


# =======================================================================================
# What a CROSS-REPO IMPORT becomes — per ecosystem, never a Bazel label
# =======================================================================================

#: `Ecosystem` → the specifier other repos write to import `com.acme:acme-lib` after it has been
#: relocated. Written out as a table on purpose: this is the one answer a rule author needs and
#: could not previously get, so changing any row must be a visible diff and not a side effect.
IMPORT_SPECIFIERS: dict[Ecosystem, str] = {
    Ecosystem.MAVEN: "com.acme.acme-lib",
    Ecosystem.GRADLE: "com.acme.acme-lib",
    Ecosystem.NPM: "com.acme/acme-lib",
    Ecosystem.PYPI: "acme_lib",
    Ecosystem.GO: "com.acme/acme-lib",
    Ecosystem.CARGO: "acme_lib",
    Ecosystem.UNKNOWN: "misc/acme-lib",
}


@pytest.mark.parametrize("eco", sorted(Ecosystem))
def test_a_cross_repo_import_becomes_a_language_name_and_never_a_bazel_label(
    eco: Ecosystem,
) -> None:
    """Every adapter answers "what does a cross-repo import of this unit become", and no answer
    is a Bazel label.

    **Why, and it is the whole of D12's first half.** §3.2 step 2's rewrite targets are import
    paths, package declarations, `tsconfig` path aliases, Go module paths, Python module paths —
    language names, every one. Bazel labels belong to §3.3, one phase later, in
    `BuildTarget.deps`. When the two were conflated, a `RewriteRule` wrote
    `import { formatMoney } from '//ts/acme/lib:lib'` into a TypeScript source and real Bazel
    reported `ts/acme/app/src/main.ts(1,29): error TS2307: Cannot find module
    '//ts/acme/lib:lib' or its corresponding type declarations` — a defect no offline test could
    see, because every layer between the rule and `tsc` was comparing our strings to our strings.

    Parametrized over the whole enum rather than spot-checked, and `import_specifier` is
    `@abstractmethod`, so a NEW language cannot silently inherit a label: it must state its own
    answer, and that answer arrives here. Both halves are asserted — the exact string, because a
    specifier that is merely "not a label" can still be the wrong name and would fail at the
    consumer's compiler; and the structural property, because the exact strings are what a
    reviewer skims past.
    """
    adapter = ecosystems.for_ecosystem(eco)
    coordinate = Coordinate(ecosystem=eco, group="com.acme", name="acme-lib")
    dest = f"{adapter.monorepo_dir}/{adapter.path_tail(coordinate)}"

    specifier = adapter.import_specifier(coordinate, dest)
    assert specifier == IMPORT_SPECIFIERS[eco], f"{adapter.name} moved its import specifier"

    # A Bazel label is `//package:target`, `@repo//package:target` or `:target`. An npm scope
    # legitimately begins with `@` and a Go module path legitimately contains `/`, so the label
    # test is the `//` root and the `:` separator — the two things no import syntax in any of
    # these languages accepts.
    assert not specifier.startswith(("//", ":")), specifier
    assert "//" not in specifier and ":" not in specifier, specifier
    assert specifier != f"//{dest}:{dest.rsplit('/', maxsplit=1)[-1]}"
    assert specifier.strip() == specifier and specifier, "a blank specifier rewrites to nothing"


def test_every_ecosystem_has_a_row_in_the_import_specifier_table() -> None:
    """**Why:** the parametrized test above would silently skip an `Ecosystem` whose row somebody
    forgot, and a missing row is exactly the case — a newly added language — where inheriting the
    wrong answer costs the most. `import_specifier` being abstract stops the adapter from being
    silent; this stops the *test* from being."""
    assert set(IMPORT_SPECIFIERS) == set(Ecosystem)


def test_every_declared_ruleset_is_pinned_by_config() -> None:
    """**Why (§9, §3.3 step 3):** `render_module_bazel` refuses to emit an unpinned `bazel_dep`,
    because an unpinned one resolves differently on the next run. An adapter naming a ruleset
    that `build.ruleset_versions` does not carry therefore fails the *whole* MODULE.bazel, for
    every language, at the first repo of its own."""
    pinned = set(BuildSection().ruleset_versions)
    for adapter in ecosystems.adapters():
        declared = {adapter.ruleset} if adapter.ruleset else set()
        declared |= {t.ruleset for t in adapter.toolchain_requirements()}
        assert declared <= pinned, f"{adapter.name} names unpinned ruleset(s) {declared - pinned}"


def test_every_extension_an_adapter_names_has_a_bzl_label() -> None:
    """**Why (D1):** `render_module_bazel` emits `<var> = use_extension(<bzl>, "<var>")`, and the
    `.bzl` label is not derivable from the module name — `rules_python` ships `pip` in
    `//python/extensions:pip.bzl`, `rules_rust` ships `crate` in `//crate_universe:extension.bzl`
    (singular), and `go_deps` is not in `rules_go` at all. The guessed `@<ruleset>//:extensions.bzl`
    that this replaces existed in one of the seven rulesets, so every other MODULE.bazel this
    harness emitted failed to load. An adapter that names an extension without recording its
    label ships that failure again, for its own language only, at its first repo.

    Structural, not a golden list: the assertion is over whatever the adapters declare, so a
    seventh adapter is covered the day it is written.
    """
    table = ecosystems.extension_bzls()
    for adapter in ecosystems.adapters():
        named = {adapter.extension} if adapter.extension else set()
        named |= {t.extension for t in adapter.toolchain_requirements()}
        for extension in sorted(named):
            var, _, tag = extension.partition(".")
            assert tag, f"{adapter.name}: {extension!r} is not '<extension>.<tag_class>'"
            assert var in table, f"{adapter.name}: no .bzl label recorded for extension {var!r}"
            assert table[var].startswith("@") and ".bzl" in table[var], table[var]


TOOLCHAINLESS_ADAPTERS = frozenset({"jvm", "unknown"})
"""The adapters that declare NO toolchain, listed rather than skipped over.

`jvm` registers none deliberately (its own docstring: a JDK comes from `rules_java`, which is not
a key in `build.ruleset_versions`, and emitting a requirement against an unpinned ruleset would
make `render_module_bazel` raise on every JVM repo). `unknown` is the §3.1 step 2 floor and
compiles nothing. Naming them is what keeps the parametrized test below from passing vacuously:
without this ledger an adapter whose `toolchain_requirements()` silently regressed to `[]` would
render nothing, assert nothing, and stay green.
"""


def _tag_call(text: str, extension: str) -> str:
    """The one `<var>.<tag>( … )` block `extension` names, out of a rendered `MODULE.bazel`.

    Sliced out rather than searching the whole file, because the file also carries `bazel_dep`
    and `single_version_override` lines full of *ruleset* versions: a toolchain version that
    happened to equal its ruleset's would make a whole-text `in` check pass on the wrong line.
    """
    var, _, tag = extension.partition(".")
    start = text.index(f"{var}.{tag}(")
    return text[start : text.index("\n)", start) + 2]


@pytest.mark.parametrize("eco", sorted(Ecosystem, key=lambda e: e.value))
def test_every_declared_toolchain_version_reaches_the_rendered_module_bazel(eco: Ecosystem) -> None:
    """**Why:** `ToolchainRequirement.version` is a *declaration*; the bytes Bazel reads are the
    tag call's `attrs`. `_tag_attrs` renders `attrs` verbatim and falls back to `name`/`version`
    only for an adapter that declared none — so an adapter with a non-empty `attrs` that spells
    its version anywhere but inside them pins nothing at all, silently, and the fleet compiles
    against whatever the ruleset defaults to.

    That is not hypothetical: `rust.py` declared `version=_RUST_VERSION, attrs={"edition": …}`
    and emitted `rust.toolchain(edition = "2021")`. The fallback firing would not have saved it
    either — `rules_rust`'s tag class has no `name`/`version` attribute, only `versions`, a
    `string_list`. Every other adapter spells its version inside `attrs` today; this is the guard
    that stops the next one from not, in whatever dialect its own ruleset uses.

    Ecosystem-neutral by construction: the assertion is that the declared version string appears
    in the rendered tag, with no attribute NAME written down here — `python_version`, `versions`,
    `ts_version` and `version` are all per-ruleset knowledge that belongs in the adapter (§13
    row 6). Adapters that declare no toolchain contribute no case and are held honest by
    `TOOLCHAINLESS_ADAPTERS` above.
    """
    adapter = ecosystems.for_ecosystem(eco)
    toolchains = adapter.toolchain_requirements()
    assert bool(toolchains) is (adapter.name not in TOOLCHAINLESS_ADAPTERS), (
        f"{adapter.name} declares {len(toolchains)} toolchain(s); TOOLCHAINLESS_ADAPTERS says "
        f"it should declare {'none' if adapter.name in TOOLCHAINLESS_ADAPTERS else 'at least one'}"
    )
    pinned = BuildSection().ruleset_versions
    for toolchain in toolchains:
        text = render_module_bazel(
            [],
            module_name="acme-monorepo",
            ruleset_versions={toolchain.ruleset: pinned[toolchain.ruleset]},
            toolchains=[toolchain],
        )
        call = _tag_call(text, toolchain.extension)
        assert toolchain.version in call, (
            f"{adapter.name}: the declared toolchain version {toolchain.version!r} does not "
            f"appear in the tag call this adapter renders, so nothing is pinned and the build "
            f"takes {toolchain.ruleset}'s default:\n{call}"
        )


@pytest.mark.parametrize("eco", sorted(Ecosystem, key=lambda e: e.value))
def test_srcs_are_package_relative_and_only_files_the_rules_accept(eco: Ecosystem) -> None:
    """**Why (D4):** both halves of a `srcs` list that real Bazel refuses, over every adapter.

    A `srcs` entry is resolved against the package that declares it, so a monorepo-root-relative
    `py/acme/x.py` inside `//py/acme` names `py/acme/py/acme/x.py` — a file that is not there, and
    an error that never mentions the doubling. And a file the rule will not compile fails ANALYSIS
    outright (`py_library`: "is misplaced here (expected .py or .py3)"), taking the package with
    it. Neither is visible to a test that compares the generator's strings to its own.

    The unit below is deliberately handed root-prefixed `srcs`, because that is the shape a
    driver that lists the tree from the monorepo root produces; the adapter re-roots them.
    """
    adapter = ecosystems.for_ecosystem(eco)
    dest = f"{adapter.monorepo_dir}/acme/widget"
    stems = ["src/Main", "src/util", "README"]
    srcs = [f"{dest}/{stem}{suffix}" for stem in stems for suffix in (".java", ".ts", ".py", ".rs")]
    unit = _unit("acme-widget", eco, dest, srcs=[*srcs, f"{dest}/pyproject.toml", f"{dest}/x.md"])

    for target in [*adapter.generate_targets(unit), *adapter.test_targets(unit)]:
        for src in target.srcs:
            assert not src.startswith(f"{dest}/"), f"{target.name}: {src} doubles the package"
            assert not src.startswith("/"), src
            assert adapter.accepts_src(src), f"{target.name}: {src} is not a {eco.value} source"
    # Nothing is silently discarded: a refused file is still an input the target carries.
    if not adapter.uses_gazelle:
        carried = {
            path
            for target in adapter.generate_targets(unit)
            for value in target.attrs.values()
            if isinstance(value, list)
            for path in value
        } | {src for target in adapter.generate_targets(unit) for src in target.srcs}
        assert "pyproject.toml" in carried or adapter.accepts_src("pyproject.toml"), carried


# =======================================================================================
# The callers stay generic: layout, SCC coarsening, BUILD rendering
# =======================================================================================


def test_layout_resolves_a_real_destination_through_the_registry() -> None:
    """**Why:** before this package existed, `bazel/layout.py` had `LayoutAdapter` /
    `EcosystemRegistry` as Protocols with test fakes only, so a repo without an explicit
    `repos.dest_path` had nowhere to go and was abandoned by `fleet build`. This asserts the real
    module satisfies the Protocol *as written* — the module itself is the registry object."""
    assert layout(LayoutNode(node_id="ops", ecosystem=Ecosystem.UNKNOWN), ecosystems) == "misc/ops"
    node = LayoutNode(
        node_id="acme-commons",
        ecosystem=Ecosystem.MAVEN,
        published=Coordinate(ecosystem=Ecosystem.MAVEN, group="com.acme", name="commons"),
    )
    assert layout(node, ecosystems) == "java/com/acme/commons"
    assert layout(LayoutNode(node_id="x", ecosystem=Ecosystem.NPM, dest_override="vendor/x"),
                  ecosystems) == "vendor/x"


def test_repointing_one_adapters_monorepo_dir_moves_every_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§13 row 33's mechanical form: re-point `NPM`'s `monorepo_dir` from `ts` to `js` and every
    `dest`, package and `//` label follows, with zero edits outside `ecosystems/js.py`.

    **Why:** this is the only executable version of §1's claim that language knowledge lives in
    one file. If any caller had copied the string, this test would still pass for `layout()` and
    fail for the SCC label — which is exactly the drift it is here to catch.

    **Why the `BuildTarget.package` leg too, and why it is driven off a real fixture repo:**
    `layout()` and `scc_label()` are two independent computations that could still both read
    `monorepo_dir` correctly while the actual `BuildUnit` a driver builds for Phase 3 carried a
    hand-typed or stale `dest` — `BuildTarget.package` is `unit.dest` verbatim
    (`ecosystems/js.py:683` etc.), copied with no re-derivation, so nothing here would notice a
    caller that stopped threading `layout()`'s answer into the unit it builds. Driving `dest`
    through a REAL `package.json` parsed by the real `NpmAdapter` — the same style
    `tests/test_manifests.py` uses for every adapter's parse tests — and then into
    `adapter.generate_targets()` closes that gap: this only passes if `layout()`,
    `generate_targets()` and `scc_label()` all resolve through the SAME re-pointed adapter, on a
    node built from bytes on disk rather than a synthetic string.
    """
    from fleet.ecosystems.js import JsAdapter
    from fleet.manifests.npm import NpmAdapter

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.ts").write_text(
        "export const version = '1.0.0';\n", encoding="utf-8"
    )
    package_json = tmp_path / "package.json"
    package_json.write_text(
        json.dumps({"name": "@acme/ui", "version": "1.0.0"}), encoding="utf-8"
    )
    coordinate = NpmAdapter().publishes(package_json)
    assert coordinate is not None, "a real package.json must yield a real published Coordinate"

    node = LayoutNode(node_id="ui", ecosystem=Ecosystem.NPM, published=coordinate)
    scc_id = "scc:0f3a1b2c3d4e5f60"

    def _package_of(dest: str) -> str:
        unit = _unit("ui", Ecosystem.NPM, dest, srcs=["src/index.ts"], published=coordinate)
        library = ecosystems.for_ecosystem(Ecosystem.NPM).generate_targets(unit)[0]
        assert library.rule == "ts_project"
        return library.package

    dest = layout(node, ecosystems)
    assert dest == "ts/acme/ui"
    assert scc_label(Ecosystem.NPM, scc_id, ecosystems).startswith("//ts/_scc/")
    assert _package_of(dest) == "ts/acme/ui"

    monkeypatch.setattr(JsAdapter, "monorepo_dir", "js")
    dest = layout(node, ecosystems)
    assert dest == "js/acme/ui"
    assert scc_label(Ecosystem.NPM, scc_id, ecosystems) == (
        "//js/_scc/scc_0f3a1b2c3d4e5f60:scc_0f3a1b2c3d4e5f60"
    )
    assert _package_of(dest) == "js/acme/ui"
    assert ecosystems.monorepo_dirs()[Ecosystem.NPM] == "js"


def test_coarse_targets_take_their_rule_names_from_the_adapters() -> None:
    """§3.1 6e + `bazel/generators.coarse_build_targets`.

    **Why:** that function raises rather than guessing when an ecosystem has no library rule, so
    before this package it could not be called at all — an `ATOMIC_WAVE` SCC had no coarsened
    target and its whole dependent cone stayed stranded. The rule map is *derived from the
    registry* rather than written at the call site, so it cannot drift from §3.3's table.
    """
    scc_id = "scc:0f3a1b2c3d4e5f60"
    plan = CoarsePlan(
        targets=(
            CoarseTarget(
                scc_id=scc_id,
                ecosystem=Ecosystem.MAVEN,
                package=f"java/_scc/scc_{scc_id.removeprefix('scc:')}",
                name=f"scc_{scc_id.removeprefix('scc:')}",
                srcs=("java/com/acme/a/A.java", "java/com/acme/b/B.java"),
                member_repo_ids=("a", "b"),
            ),
        ),
        standalone_repo_ids=(),
        merged_repo_ids=("a", "b"),
        findings=(),
    )
    (target,) = coarse_build_targets(
        plan, ecosystems.library_rules(), load_from=ecosystems.library_loads()
    )
    assert target.rule == "java_library"
    assert target.load_from == "@rules_java//java:defs.bzl"
    assert ecosystems.library_rules()[Ecosystem.UNKNOWN] == "filegroup"
    assert Ecosystem.UNKNOWN not in ecosystems.library_loads(), "filegroup is native; no load()"


def test_generated_targets_render_to_a_valid_build_file() -> None:
    """**Why:** an adapter's output is only worth anything if `render_build_bazel` accepts it —
    one package per file, a `load()` for every non-native rule. A target whose `package` differs
    from its unit's `dest` is rejected there, and this is where that contract is exercised
    end-to-end rather than assumed."""
    adapter = ecosystems.for_ecosystem(Ecosystem.PYPI)
    unit = _unit(
        "acme-svc", Ecosystem.PYPI, "py/acme-svc",
        srcs=["acme_svc/__init__.py"], test_srcs=["tests/test_client.py"],
    )
    text = render_build_bazel(adapter.generate_targets(unit) + adapter.test_targets(unit))
    assert 'load("@rules_python//python:defs.bzl", "py_library", "py_test")' in text
    assert "py_library(" in text and "py_test(" in text
    assert "GENERATED BY fleet" in text


def _a_declared_dependency(eco: Ecosystem) -> Coordinate:
    """One external dependency shaped the way `eco`'s OWN manifests would have recorded it.

    **Why this is not one literal for every ecosystem any more.** `left-pad ^1.3.0` is an npm
    coordinate, and Go is the one ecosystem where the root file is a *grammar* rather than a list:
    the fleet's `go.mod` is both what lands at the root and what `go mod download all` reads, so
    `go.py` now refuses to write a `require` line `go` would reject at parse time and raises
    `GoModuleCoordinateError` naming the coordinate instead (ADR-0050 step 2, and
    `test_a_go_coordinate_that_cannot_be_a_require_line_fails_loudly` is the assertion on that).
    Handing the Go adapter an npm coordinate here would exercise that loud failure rather than the
    structural contract this test is about. Every other adapter writes a name and a spec into a
    file with no grammar to violate, so their literal is unchanged.
    """
    if eco is Ecosystem.GO:
        return Coordinate(
            ecosystem=eco, group="github.com/acme", name="left-pad", version_spec="v1.3.0"
        )
    return Coordinate(ecosystem=eco, name="left-pad", version_spec="^1.3.0")


@pytest.mark.parametrize("eco", sorted(Ecosystem, key=lambda e: e.value))
def test_a_declared_resolver_names_a_lockfile_the_adapter_actually_references(
    eco: Ecosystem,
) -> None:
    """Every `resolution()` an adapter declares produces a file that same adapter's MODULE.bazel
    tags NAME, from an argv that is a real command and inputs that are not empty.

    **Why:** a lockfile is a resolution, and the harness had never run a resolver — the file
    `pip.parse(requirements_lock = "//:requirements.lock")` read was the *declared specs*, which
    carry no transitive closure, so real Bazel reported `no such package
    '@@rules_python++pip+pypi//certifi' … referenced by
    '@@rules_python++pip+pypi_312_requests//:pkg'` about a distribution nothing in the fleet
    declares. `Resolution` closes that, and this is the structural guard on it: a resolver whose
    `lock_path` is not one of the adapter's own `workspace_files()`/`package_files()` writes a
    file nothing reads, which is the same class of unkept promise D10 was.

    Parameterized over every `Ecosystem` so an adapter added tomorrow is covered by construction,
    and it asserts nothing about adapters that declare no resolver: `maven.install` pins per
    artifact and names no lockfile, and "this dialect needs no resolution" is a real answer.
    """
    adapter = ecosystems.for_ecosystem(eco)
    unit = _unit(
        "acme-widget",
        eco,
        f"{adapter.monorepo_dir}/acme/widget",
        srcs=["src/index.ts", "widget.py", "Widget.java", "main.go", "src/lib.rs", "README.md"],
        external=[_a_declared_dependency(eco)],
    )
    plan = adapter.resolution([unit])
    if plan is None:
        return
    declared = {f.path for f in [*adapter.workspace_files([unit]), *adapter.package_files(unit)]}
    assert plan.lock_path in declared, (
        f"{adapter.name}: resolves {plan.lock_path!r}, which is not a file its generated files "
        f"name ({sorted(declared)}) — a resolved lock nothing references is never read"
    )
    assert plan.argv and not any(" " in part for part in plan.argv[:1]), plan.argv
    assert plan.inputs, "a resolver with no inputs resolves nothing"
    for source in plan.inputs:
        assert source.content or source.carry_from, source
    # …and no resolver at all for a unit with no external dependencies: there is no lockfile to
    # produce, and running one over an empty input writes the empty lock this whole path exists
    # to stop being written.
    assert adapter.resolution([_unit("bare", eco, unit.dest, srcs=["widget.py"])]) is None


def test_the_resolution_an_adapter_declares_is_identical_across_two_calls() -> None:
    """`resolution()` is pure — same unit, same argv, same input bytes.

    **Why (§11.6):** the resolver's inputs decide the lockfile, the lockfile is committed onto the
    integration branch, and a lockfile is an input to the run-equivalence digest's world. An
    adapter that folded a timestamp, a set iteration or an absolute path into `requirements.in`
    would make two runs of the same fleet resolve two different closures with no diff anywhere in
    this repository — the same failure mode as an unpinned `bazel_dep`.
    """
    adapter = ecosystems.for_ecosystem(Ecosystem.PYPI)
    unit = _unit(
        "acme-svc",
        Ecosystem.PYPI,
        "py/acme-svc",
        srcs=["acme_svc/__init__.py"],
        external=[
            Coordinate(ecosystem=Ecosystem.PYPI, name="requests", version_spec=">=2.31"),
            Coordinate(ecosystem=Ecosystem.PYPI, name="httpx", version_spec=">=0.27"),
        ],
    )
    first, second = adapter.resolution([unit]), adapter.resolution([unit])
    assert first is not None and second is not None
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    (source,) = first.inputs
    assert "requests>=2.31" in source.content and "httpx>=0.27" in source.content
    # The output path is the SAME string `pip.parse` names, read off the adapter rather than
    # spelled here — the two drifting apart is defect D10 in one line.
    (lock,) = adapter.workspace_files([unit])
    assert first.lock_path == lock.path


def test_the_go_resolution_is_identical_across_two_calls() -> None:
    """The same purity claim for Go, whose `Resolution` is a different shape (ADR-0050).

    **Why this needs its own case and is not covered by the Python one.** Python's resolver input
    is a flat spec list; Go's is a `go.mod` — a module declaration plus a `require` body unioned
    over every Go unit, and the same bytes that land at `//:go.mod`. Purity is therefore a claim
    about two files at once: a union taken by set iteration would resolve one ordering of the
    requirements in one process and another in the next, from the same plan, with no diff anywhere
    in this repository, and because every hash in each resulting `go.sum` would be individually
    valid the only visible symptom would be a checksum mismatch inside Bazel blaming the module
    cache.

    `model_dump(mode="json")` rather than `==` on the models, so a field added to `Resolution`
    later is covered without this test being edited.
    """
    adapter = ecosystems.for_ecosystem(Ecosystem.GO)
    unit = _unit(
        "acme-commons-go",
        Ecosystem.GO,
        "go/commons",
        srcs=["commons.go"],
        published=Coordinate(ecosystem=Ecosystem.GO, group="github.com/acme", name="commons"),
        external=[
            Coordinate(
                ecosystem=Ecosystem.GO,
                group="github.com/stretchr",
                name="testify",
                version_spec="v1.9.0",
            ),
            Coordinate(
                ecosystem=Ecosystem.GO,
                group="github.com/google",
                name="uuid",
                version_spec="v1.6.0",
            ),
        ],
    )
    first, second = adapter.resolution([unit]), adapter.resolution([unit])
    assert first is not None and second is not None
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    (source,) = first.inputs
    assert source.carry_from == [], "no repo's own go.mod is the fleet's union of them"
    root = next(f for f in adapter.workspace_files([unit]) if f.path == "go.mod")
    assert source.content == root.content, (
        "the sums are hashes taken against THIS file; a resolver input that differs from what "
        "lands at the root is a checksum mismatch inside Bazel that reads as a compromise"
    )
    # The output path is the same string the root file declares, read off the adapter — and it is
    # the `go.sum` one, not the `go.mod` one, which is why this indexes rather than unpacks.
    declared = {f.path for f in adapter.workspace_files([unit])}
    assert first.lock_path in declared and declared == {"go.mod", "go.sum"}
    # …and purity now has to hold over the environment too, because `GOTOOLCHAIN` decides which
    # SDK computes the sums and is therefore an input to the lockfile exactly as the argv is.
    assert first.env == second.env


def test_a_resolution_declares_no_environment_unless_it_needs_one() -> None:
    """`Resolution.env` defaults to empty, round-trips through JSON, and is per-instance.

    **Why the default matters as much as the field:** the environment is an input to the bytes a
    resolver writes, so a model whose default were anything but "declare nothing" would make
    every adapter that never thought about the environment silently ship one. Empty means "the
    driver overlays nothing", which is exactly the pre-existing behaviour for `uv` and `pnpm`.

    **Why the mutable-default check is here and not assumed:** a plain `= {}` on a Pydantic field
    is a class-level object, and two `Resolution`s sharing one dict would let the Go adapter's
    `GOTOOLCHAIN` leak into the Python resolver's environment — a cross-ecosystem contamination
    with no diff anywhere in this repository. `default_factory` is what stops that, and this is
    the assertion that would catch its removal.
    """
    bare = Resolution(lock_path="requirements.lock", argv=["uv", "pip", "compile"])
    assert bare.env == {}
    other = Resolution(lock_path="go.sum", argv=["go", "mod", "download", "all"])
    bare.env["GOTOOLCHAIN"] = "go1.24.12"
    assert other.env == {}, "a shared class-level default leaks one adapter's env into another's"

    pinned = Resolution(
        lock_path="go.sum",
        argv=["go", "mod", "download", "all"],
        env={"GOTOOLCHAIN": "go1.24.12"},
    )
    dumped = pinned.model_dump(mode="json")
    assert dumped["env"] == {"GOTOOLCHAIN": "go1.24.12"}
    assert Resolution.model_validate(json.loads(pinned.model_dump_json())) == pinned


def test_a_resolution_built_by_a_real_adapter_round_trips_and_validates() -> None:
    """`Resolution` (SPEC §12.46(ii)) is built by all three ecosystem adapters that need one
    (py/js/go) and had zero coverage of its own — the tests above exercise `adapter.resolution()`
    for its argv/inputs/env shape, but never round-trip or validate the instance the adapter
    actually hands back. Built via the real `PyAdapter.resolution()` path (not raw construction),
    so this proves the object the driver receives — not a hand-shaped stand-in of it — survives
    its own checkpoint format.

    Compared field-by-field via `model_fields`, per the same §12.46(i) mechanism `test_state_
    models.py` uses for every other durable model — `Resolution` is not exported by `fleet.models`
    (adapters import it directly from `fleet.models.build`, ADR-0020 §7.5's adapter-purity seam),
    so it never entered that file's `SAMPLES`/`EXPORTED` registry; this is its dedicated coverage.
    """
    adapter = ecosystems.for_ecosystem(Ecosystem.PYPI)
    unit = _unit(
        "acme-svc",
        Ecosystem.PYPI,
        "py/acme-svc",
        srcs=["acme_svc/__init__.py"],
        external=[
            Coordinate(ecosystem=Ecosystem.PYPI, name="requests", version_spec=">=2.31"),
            Coordinate(ecosystem=Ecosystem.PYPI, name="httpx", version_spec=">=0.27"),
        ],
    )
    built = adapter.resolution([unit])
    assert built is not None

    payload = json.loads(built.model_dump_json())
    reloaded = Resolution.model_validate(payload)
    for field_name in Resolution.model_fields:
        original_value = getattr(built, field_name)
        reloaded_value = getattr(reloaded, field_name)
        assert reloaded_value == original_value, (
            f"Resolution.{field_name} did not round-trip: {reloaded_value!r} != {original_value!r}"
        )

    # Validation properties: `argv` (the resolver command) and `lock_path` (what it writes) are
    # both `min_length=1` — an adapter-built `Resolution` with either emptied is not a real
    # instance a driver could act on, and must be rejected on the way back in exactly as a
    # hand-constructed one would be (§12.46(i)'s round-trip boundary applies here too).
    with pytest.raises(ValidationError):
        Resolution.model_validate(payload | {"argv": []})
    with pytest.raises(ValidationError):
        Resolution.model_validate(payload | {"lock_path": ""})

    # extra="forbid" (FleetModel, ADR-0002) — an adapter-built Resolution is no more tolerant of
    # a renamed/unknown field on the way back in than any hand-constructed one.
    with pytest.raises(ValidationError):
        Resolution.model_validate(payload | {"totally_unknown": 1})


def test_the_go_resolver_pins_the_toolchain_to_the_sdk_bazel_will_fetch() -> None:
    """Go's `Resolution` declares `GOTOOLCHAIN=go<version>`, from the SAME constant the registered
    `go_sdk` toolchain carries.

    **Why (ADR-0050's named gap):** `go mod download all` writes hashes *of the module graph the
    toolchain it ran under resolved*, and which toolchain that is comes out of `GOTOOLCHAIN`,
    whose default is `auto`. Measured on a real host: with `auto` a `go.mod` requiring a newer Go
    than the local one causes `go` to silently download and switch to that newer toolchain and
    succeed; with `local` the identical inputs fail hard (`go.mod requires go >= 1.23.4 (running
    go 1.22.2; GOTOOLCHAIN=local)`). So without this pin the sums this harness commits are a
    function of an operator's shell variable that no run records and no test controls.

    Asserted against `toolchain_requirements()` rather than a literal, because the property that
    matters is not "some version" but *the same* version: `go_sdk.download` fetches the SDK that
    later verifies these sums, and a resolver pinned to a different one produces a checksum
    mismatch inside Bazel — a failure that reads as a supply-chain compromise and that the
    harness would have manufactured itself.
    """
    adapter = ecosystems.for_ecosystem(Ecosystem.GO)
    unit = _unit(
        "acme-commons-go",
        Ecosystem.GO,
        "go/commons",
        srcs=["commons.go"],
        published=Coordinate(ecosystem=Ecosystem.GO, group="github.com/acme", name="commons"),
        external=[
            Coordinate(
                ecosystem=Ecosystem.GO,
                group="github.com/stretchr",
                name="testify",
                version_spec="v1.9.0",
            )
        ],
    )
    plan = adapter.resolution([unit])
    assert plan is not None
    (sdk,) = adapter.toolchain_requirements()
    assert plan.env == {"GOTOOLCHAIN": f"go{sdk.version}"}, (
        f"the resolver must run under the SDK `go_sdk.download` registers ({sdk.version}); "
        f"declared {plan.env}"
    )


def _go_unit(name: str, dest: str, group: str, module: str, version: str) -> BuildUnit:
    """A Go repo publishing `github.com/acme/<name>` and requiring exactly one external module."""
    return _unit(
        name,
        Ecosystem.GO,
        dest,
        srcs=["main.go"],
        published=Coordinate(ecosystem=Ecosystem.GO, group="github.com/acme", name=name),
        external=[
            Coordinate(ecosystem=Ecosystem.GO, group=group, name=module, version_spec=version)
        ],
    )


def test_the_go_root_module_is_the_monorepos_own_and_unions_both_repos_requires() -> None:
    """Two Go repos, ONE root `go.mod`, and its `require` block holds BOTH of their modules.

    **Why (ADR-0050 step 2).** `workspace_files` used to route through `union_workspace_files`,
    whose merge is first-writer-wins by `setdefault` on `path`, and each unit contributed a whole
    root `go.mod` rendered from *its own* coordinates. With two Go repos the second unit's file
    was discarded in silence: the root declared one repo's module path and one repo's `require`
    block, and repo B's dependencies did not exist in the module graph at all. Nothing could raise
    on it — `cli._fleet_support_files` hands every plan of an ecosystem the identical tuple, so
    `RootFileConflictError` never sees two candidates and the drop happens upstream of the guard.
    This is the third instance of that shape, after the pnpm workspace (ADR-0048) and the Cargo
    workspace (ADR-0049).

    **The `module` line is asserted to be NEITHER repo's**, which is the half that is easy to
    mistake for cosmetic. `deps_from_go_mod` consumes it only as the main module path while the
    `require` list is what becomes the `@com_github_…` repos, so unioning the requires is the term
    that carries the dependencies — but a `module` line naming one repo makes the fleet's root
    file a claim about that repo, and the next reader "fixes" the union back into it. Import paths
    are not hostage to it: `# gazelle:prefix` is written per package, and that is asserted here
    against the same units so the two facts cannot drift apart.

    **Order-independence (§11.6)** because these bytes are committed onto the integration branch
    and are an input to the run-equivalence digest; a union taken by iteration would make two runs
    of the same fleet differ with no diff anywhere in this repository.
    """
    adapter = ecosystems.for_ecosystem(Ecosystem.GO)
    commons = _go_unit("commons", "go/commons", "github.com/stretchr", "testify", "v1.9.0")
    server = _go_unit("server", "go/server", "github.com/google", "uuid", "v1.6.0")

    files = {f.path: f for f in adapter.workspace_files([commons, server])}
    assert set(files) == {"go.mod", "go.sum"}
    text = files["go.mod"].content
    assert "\tgithub.com/stretchr/testify v1.9.0" in text, text
    assert "\tgithub.com/google/uuid v1.6.0" in text, (
        f"the second repo's requirements never reached the module graph: {text}"
    )
    assert "module fleet.internal/monorepo" in text, text
    for unit in (commons, server):
        published = f"github.com/acme/{unit.unit_id}"
        assert f"module {published}" not in text, (
            f"the fleet's root module is not {published}; a root file that names one repo is "
            f"the collision in its other half"
        )
        assert f"# gazelle:prefix {published}" in adapter.gazelle_config(unit).directives, (
            "each repo keeps its own import path under a monorepo-rooted module"
        )
    assert files["go.mod"].carry_from == [], (
        "one repo's go.mod declares THAT repo's module and requirements; carrying it to the root "
        "describes a monorepo that does not exist — and `cli._carried` would skip the resolver"
    )

    reverse = adapter.workspace_files([server, commons])
    assert [f.model_dump(mode="json") for f in adapter.workspace_files([commons, server])] == [
        f.model_dump(mode="json") for f in reverse
    ]


def test_two_go_repos_pinning_one_module_differently_leave_the_choice_to_gos_mvs() -> None:
    """Both pins reach the root `go.mod`; neither is dropped and neither is reconciled here.

    **Why not pick the higher one in this harness.** `workspace_deps` states the rule the whole
    adapter is built on: Go's MVS already ran when each `go.mod` was written, and a reconciliation
    performed *here* is one `go build` and `bazel build` can disagree about. Emitting both lines
    hands the choice to the toolchain that owns it — measured against real `go`, which loaded a
    `go.mod` requiring `testify` at both `v1.9.0` and `v1.8.0`, reported no error, and selected
    `v1.9.0`. Dropping the loser here would be the same silent drop this file is about, one level
    down: the surviving pin would be an artifact of which repo sorted first.
    """
    adapter = ecosystems.for_ecosystem(Ecosystem.GO)
    older = _go_unit("commons", "go/commons", "github.com/stretchr", "testify", "v1.8.0")
    newer = _go_unit("server", "go/server", "github.com/stretchr", "testify", "v1.9.0")
    text = next(f for f in adapter.workspace_files([older, newer]) if f.path == "go.mod").content
    pins = sorted(line.strip() for line in text.splitlines() if "testify" in line)
    assert pins == ["github.com/stretchr/testify v1.8.0", "github.com/stretchr/testify v1.9.0"], (
        f"one of the two pins was dropped before Go's MVS ever saw both: {pins}"
    )


def test_a_go_coordinate_that_cannot_be_a_require_line_fails_loudly() -> None:
    """A coordinate claiming `Ecosystem.GO` that `go` would reject raises and NAMES itself.

    **Why loud and not skipped, and not written out anyway (Rule 11).** The root `go.mod` is now
    both what lands at `//:go.mod` and what `go mod download all` reads, so it has to be a file
    `go` accepts. Writing an unrenderable coordinate out is the worse of the two silent options:
    `go` refuses a malformed module path at PARSE time, so one bad coordinate takes down every
    other Go repo's requirements with it, and the message names a file this harness invented
    rather than the repo whose manifest produced the coordinate. Skipping it is the other silent
    option and is the defect this whole round is about. Raising names the coordinate, which is the
    one thing an operator can act on.

    **The version half is asserted too, and `v0.0.0` specifically.** The previous renderer
    defaulted a version-less coordinate to `v0.0.0`, which reads as a floor and is not one: `go`
    answers `invalid version: unknown revision v0.0.0` (ADR-0050, measured). Defaulting only moved
    the failure into the resolver and dropped the coordinate's identity on the way.

    **A NON-Go coordinate is excluded rather than raised on, and that is a different rule.** A
    `BuildUnit`'s `external_coordinates` are re-read from every manifest the repo ships, so a
    Go-primary repo with a `package.json` carries npm coordinates here; an npm package is not a Go
    module in any rendering, and the JS adapter's own root files are where it is expressed. That
    exclusion is what stopped `left-pad ^1.3.0` being written into the fleet's root module file.

    The exception is read off the MODULE on every use, never bound at import: the `registry`
    fixture's teardown runs `reset_adapters()` + `discover(force=True)`, which `importlib.reload`s
    every adapter module and rebinds its classes — so a module-level `from … import` here would
    hold a stale class object and `pytest.raises` would stop matching after the first test in the
    file, which is a green suite asserting nothing.
    """
    adapter = ecosystems.for_ecosystem(Ecosystem.GO)
    unrenderable = go_adapter.GoModuleCoordinateError
    dotless = _go_unit("commons", "go/commons", "", "left-pad", "v1.3.0")
    with pytest.raises(unrenderable, match="left-pad"):
        adapter.workspace_files([dotless])
    with pytest.raises(unrenderable, match="left-pad"):
        adapter.resolution([dotless])

    unversioned = _unit(
        "commons",
        Ecosystem.GO,
        "go/commons",
        srcs=["main.go"],
        external=[Coordinate(ecosystem=Ecosystem.GO, group="github.com/google", name="uuid")],
    )
    with pytest.raises(unrenderable, match=r"v0\.0\.0"):
        adapter.workspace_files([unversioned])
    floored = _go_unit("commons", "go/commons", "github.com/google", "uuid", "v0.0.0")
    with pytest.raises(unrenderable, match=r"v0\.0\.0"):
        adapter.workspace_files([floored])

    # …and the npm coordinate a polyglot Go repo carries is simply not a `require`: the root file
    # is still written, still valid, and declares no `go.sum` because there is nothing to verify.
    polyglot = _unit(
        "commons",
        Ecosystem.GO,
        "go/commons",
        srcs=["main.go"],
        external=[Coordinate(ecosystem=Ecosystem.NPM, name="left-pad", version_spec="^1.3.0")],
    )
    (root,) = adapter.workspace_files([polyglot])
    assert root.path == "go.mod" and "left-pad" not in root.content, root.content
    assert "require (" not in root.content, root.content
    assert adapter.resolution([polyglot]) is None, (
        "`sums_from_go_mod` is reached only for a go.mod carrying a require; resolving one "
        "without any writes no go.sum and would surface as a resolver failure"
    )


def test_the_python_and_js_resolvers_declare_no_environment() -> None:
    """`uv` and `pnpm` stay on the inherited environment — this round retunes only Go.

    **Why assert an emptiness:** adding `Resolution.env` is exactly the kind of change that
    invites a speculative `{"UV_INDEX_URL": …}` or `{"npm_config_registry": …}` alongside it, and
    either one would silently change which index the fleet's locks resolve against for every
    Python or JS repo. The environment these two run under is the operator's, unchanged, and
    that is a claim worth failing on rather than a gap.
    """
    for eco, tool in ((Ecosystem.PYPI, "uv"), (Ecosystem.NPM, "pnpm")):
        adapter = ecosystems.for_ecosystem(eco)
        unit = _unit(
            "acme-widget",
            eco,
            f"{adapter.monorepo_dir}/acme/widget",
            srcs=["src/index.ts", "widget.py"],
            external=[Coordinate(ecosystem=eco, name="left-pad", version_spec="^1.3.0")],
        )
        plan = adapter.resolution([unit])
        assert plan is not None, eco
        assert plan.argv[0] == tool, plan.argv
        assert plan.env == {}, f"{eco.value}: {plan.env}"


def _js_unit(name: str, package: str, sibling: str) -> BuildUnit:
    """A JS unit with one registry dependency and one linkable sibling, both its own."""
    return _unit(
        name,
        Ecosystem.NPM,
        f"ts/acme/{name}",
        srcs=["src/index.ts"],
        external=[Coordinate(ecosystem=Ecosystem.NPM, name=package, version_spec="^1.0.0")],
        internal_deps=[
            InternalDep(
                label=f"//ts/acme/{sibling}:{sibling}",
                dest=f"ts/acme/{sibling}",
                published=Coordinate(
                    ecosystem=Ecosystem.NPM, group="@acme", name=sibling, version_spec="1.0.0"
                ),
            )
        ],
    )


def test_js_resolution_unions_every_units_dependencies_into_one_workspace() -> None:
    """Two JS repos, ONE resolve — and ONE `package.json` **per repo**, each keeping its own.

    **Why:** `npm_translate_lock` reads a single `//:pnpm-lock.yaml`, so the resolve that produces
    it is per-ECOSYSTEM and not per-repo. A workspace built from one unit resolves a lock in which
    the other repo's `left-pad` does not appear, and real Bazel answers with `no such target
    '//ts/acme/web:node_modules/left-pad'` for a dependency the fleet plainly declared — from a
    file nobody in this repository wrote. The sibling half is the same failure one layer down: a
    missing `link:` entry is a first-party package that never becomes a `node_modules` entry.

    **And the union is a WORKSPACE, not a flat map** (ADR-0048). The root manifest carries no
    dependencies at all; each repo's live under that repo's `dest`. The flat map this replaced
    was keyed on package name, so unioning two units overwrote in place and `[app ms@^2.0.0,
    report ms@^2.1.3]` emitted `{"ms": "^2.1.3"}` — the loser gone, with no error, and the winner
    merely the lexicographically larger specifier.
    """
    adapter = ecosystems.for_ecosystem(Ecosystem.NPM)
    first = _js_unit("app", "left-pad", "tokens")
    second = _js_unit("web", "react", "icons")

    plan = adapter.resolution([first, second])
    assert plan is not None
    inputs = {support.path: support for support in plan.inputs}
    assert "dependencies" not in json.loads(inputs["package.json"].content)
    assert json.loads(inputs["ts/acme/app/package.json"].content)["dependencies"] == {
        "left-pad": "^1.0.0",
        "@acme/tokens": "link:../tokens",
    }
    assert json.loads(inputs["ts/acme/web/package.json"].content)["dependencies"] == {
        "react": "^1.0.0",
        "@acme/icons": "link:../icons",
    }
    # Each `link:` specifier is resolved by READING the manifest at that directory, so every
    # sibling of every unit owes an input — the resolver exits non-zero naming the path otherwise.
    assert "ts/acme/tokens/package.json" in inputs
    assert "ts/acme/icons/package.json" in inputs
    # …and every one of those directories is a `packages:` member, or pnpm never reads it at all.
    workspace = inputs["pnpm-workspace.yaml"].content
    for dest in ("ts/acme/app", "ts/acme/web", "ts/acme/tokens", "ts/acme/icons"):
        assert f"  - {dest}\n" in workspace, workspace


def test_two_js_units_declaring_one_package_at_two_specs_keep_both_specifiers() -> None:
    """The defect ADR-0048 names, at the level it happens: `ms@^2.0.0` and `ms@^2.1.3` are two
    facts and the resolver's input must carry both.

    **Why this is the assertion.** The flat union was a `dict` keyed on the npm package name, so
    the second unit's entry *overwrote* the first's in place — no exception, no finding, and a
    root manifest that says the fleet declared one specifier when it declared two. Real pnpm then
    resolves a workspace that is missing a repo's declaration, and Bazel reports it as `no such
    target '//<dest>:node_modules/<pkg>'` about a package the fleet plainly named. Asserted on
    both importers rather than on the count, because "two entries survived" would also pass if
    both said `^2.1.3`.
    """
    adapter = ecosystems.for_ecosystem(Ecosystem.NPM)
    older = _unit(
        "app",
        Ecosystem.NPM,
        "ts/acme/app",
        srcs=["src/index.ts"],
        external=[Coordinate(ecosystem=Ecosystem.NPM, name="ms", version_spec="^2.0.0")],
    )
    newer = _unit(
        "report",
        Ecosystem.NPM,
        "ts/acme/report",
        srcs=["src/index.ts"],
        external=[Coordinate(ecosystem=Ecosystem.NPM, name="ms", version_spec="^2.1.3")],
    )

    plan = adapter.resolution([older, newer])
    assert plan is not None
    inputs = {support.path: support for support in plan.inputs}
    assert json.loads(inputs["ts/acme/app/package.json"].content)["dependencies"] == {
        "ms": "^2.0.0"
    }
    assert json.loads(inputs["ts/acme/report/package.json"].content)["dependencies"] == {
        "ms": "^2.1.3"
    }
    # Both repos are importers, so both owe a `.bazelignore` line — `_find_missing_bazel_ignores`
    # in `npm_translate_lock_helpers.bzl` fails the repository rule for every importer whose
    # `<importer>/node_modules` is not an exact line, and a bare `node_modules` does not cover a
    # subdirectory.
    files = {f.path: f.content for f in adapter.workspace_files([older, newer])}
    assert files[".bazelignore"].splitlines() == [
        "node_modules",
        "ts/acme/app/node_modules",
        "ts/acme/report/node_modules",
    ]
    # …and each repo's own targets name the links its own importer declares, never the root's.
    assert adapter.external_labels(older) == ["//ts/acme/app:node_modules/ms"]
    assert adapter.external_labels(newer) == ["//ts/acme/report:node_modules/ms"]


def test_js_resolution_is_byte_identical_whichever_order_the_units_arrive_in() -> None:
    """**Why (§11.6):** the driver groups the units; nothing orders them. If the union were taken
    by iteration, the resolved lock — a file committed onto the integration branch and an input to
    the run-equivalence digest — would be a function of that grouping, and two runs of the same
    fleet would differ with no diff anywhere in this repository."""
    adapter = ecosystems.for_ecosystem(Ecosystem.NPM)
    first = _js_unit("app", "left-pad", "tokens")
    second = _js_unit("web", "react", "icons")

    forward, reverse = adapter.resolution([first, second]), adapter.resolution([second, first])
    assert forward is not None and reverse is not None
    assert forward.model_dump(mode="json") == reverse.model_dump(mode="json")


def test_js_resolution_survives_a_unit_that_needs_no_hub_beside_one_that_does() -> None:
    """The `any(...)` gate: a unit with neither an external dep nor a linkable sibling contributes
    nothing to the manifest and must not veto the resolve for the units beside it — which is what
    a gate reading only the first unit would do."""
    adapter = ecosystems.for_ecosystem(Ecosystem.NPM)
    bare = _unit("docs", Ecosystem.NPM, "ts/acme/docs", srcs=["src/index.ts"])
    real = _js_unit("app", "left-pad", "tokens")

    plan = adapter.resolution([bare, real])
    assert plan is not None
    inputs = {support.path: support for support in plan.inputs}
    assert json.loads(inputs["ts/acme/app/package.json"].content)["dependencies"] == {
        "left-pad": "^1.0.0",
        "@acme/tokens": "link:../tokens",
    }
    # …and the unit that needs no hub is not a `packages:` member either: it contributes no
    # manifest, so listing it would point pnpm at a directory with nothing in it.
    assert "ts/acme/docs" not in inputs["pnpm-workspace.yaml"].content
    assert adapter.resolution([bare]) is None


def test_js_workspace_files_hold_one_entry_per_root_path_across_two_units() -> None:
    """Two JS repos, ONE entry per monorepo-root path — not two.

    **Why (ADR-0048):** `pnpm-lock.yaml`, `pnpm-workspace.yaml` and `.bazelignore` are files at
    the monorepo ROOT, so the fleet holds exactly one of each however many JS repos name them. A
    union that kept a `SupportFile` per unit would hand the driver two candidates for one path,
    and `cli._module_inputs`' first-writer-wins `setdefault` would silently drop one — which is
    precisely how a root file that must state a fleet-wide fact ends up stating one repo's.
    """
    adapter = ecosystems.for_ecosystem(Ecosystem.NPM)
    first = _js_unit("app", "left-pad", "tokens")
    second = _js_unit("web", "react", "icons")

    files = adapter.workspace_files([first, second])
    paths = [support.path for support in files]
    assert sorted(paths) == sorted(set(paths)), f"a root path appears twice: {paths}"
    assert set(paths) == {".bazelignore", "pnpm-lock.yaml", "pnpm-workspace.yaml"}
    # The one-unit view is the same set — widening the arity must not add or drop a file.
    assert {support.path for support in adapter.workspace_files([first])} == set(paths)


def test_js_workspace_files_are_identical_whichever_order_the_units_arrive_in() -> None:
    """**Why (§11.6):** the driver groups the units and nothing orders them, so a union taken by
    iteration would make the monorepo's root files — carried onto the integration branch and an
    input to the run-equivalence digest — a function of that grouping. Two runs of the same fleet
    would then differ with no diff anywhere in this repository."""
    adapter = ecosystems.for_ecosystem(Ecosystem.NPM)
    first = _js_unit("app", "left-pad", "tokens")
    second = _js_unit("web", "react", "icons")

    forward = adapter.workspace_files([first, second])
    reverse = adapter.workspace_files([second, first])
    assert [support.model_dump(mode="json") for support in forward] == [
        support.model_dump(mode="json") for support in reverse
    ]


def test_py_resolution_unions_every_units_specs_into_one_requirements_in() -> None:
    """The same widening for `pip.parse`: one `//:requirements.lock`, one `uv pip compile` over
    every Python unit's specs.

    **Why:** a per-repo resolve produces N candidate locks for ONE path, the driver keeps the
    first, and every other repo's distributions are absent from the hub its `@pypi//<pkg>` labels
    resolve in. Order-independence is asserted for the reason §11.6 gives.
    """
    adapter = ecosystems.for_ecosystem(Ecosystem.PYPI)
    first = _unit(
        "acme-svc",
        Ecosystem.PYPI,
        "py/acme-svc",
        srcs=["acme_svc/__init__.py"],
        external=[Coordinate(ecosystem=Ecosystem.PYPI, name="requests", version_spec=">=2.31")],
    )
    second = _unit(
        "acme-cli",
        Ecosystem.PYPI,
        "py/acme-cli",
        srcs=["acme_cli/__init__.py"],
        external=[Coordinate(ecosystem=Ecosystem.PYPI, name="httpx", version_spec=">=0.27")],
    )

    plan = adapter.resolution([first, second])
    assert plan is not None
    (source,) = plan.inputs
    assert "requests>=2.31" in source.content
    assert "httpx>=0.27" in source.content

    reverse = adapter.resolution([second, first])
    assert reverse is not None
    assert plan.model_dump(mode="json") == reverse.model_dump(mode="json")


def test_py_root_lock_carries_for_one_unit_and_stops_carrying_for_two() -> None:
    """`//:requirements.lock` keeps its `carry_from` while ONE unit contributes and drops it the
    moment a second does — and its floor unions both units' specs either way.

    **Why:** `pip.parse(requirements_lock = "//:requirements.lock")` builds ONE `@pypi` hub from
    ONE file. With one contributor that file is a resolution of that repo's own dependency set,
    so the repo's tested lock is the honest content and re-resolving it would silently move pins
    it shipped. With two it is a resolution of the UNION, which no single repo's lock is — and
    `cli._carried` short-circuits the resolver entirely whenever a carry candidate exists, so
    leaving the attribute on promoted one repo's single-package lock to the whole fleet with no
    resolver ever running and no error anywhere. Worse, the candidate offered was the
    lexicographically first `dest`, so which repo's pins survived was an artifact of string
    ordering: `py/acme-cli` beats `py/acme_svc` because `-` sorts before `_`.

    Every carry candidate is asserted to live under the contributing unit's own `dest`, because
    that — and not the file's name — is what makes it *that repo's* resolution: a candidate at
    the monorepo root would be the harness reading a file it wrote itself.
    """
    adapter = ecosystems.for_ecosystem(Ecosystem.PYPI)
    first = _unit(
        "acme-svc",
        Ecosystem.PYPI,
        "py/acme_svc",
        srcs=["acme_svc/__init__.py"],
        external=[Coordinate(ecosystem=Ecosystem.PYPI, name="requests", version_spec=">=2.31")],
    )
    second = _unit(
        "acme-cli",
        Ecosystem.PYPI,
        "py/acme-cli",
        srcs=["acme_cli/__init__.py"],
        external=[Coordinate(ecosystem=Ecosystem.PYPI, name="httpx", version_spec=">=0.27")],
    )

    (alone,) = adapter.workspace_files([first])
    assert "py/acme_svc/requirements.lock" in alone.carry_from, (
        "one contributor: the union IS this repo's dependency set, so its own tested resolution "
        f"is the honest content of the root lock — {alone.carry_from}"
    )
    assert all(candidate.startswith("py/acme_svc/") for candidate in alone.carry_from), (
        f"a carry candidate outside the contributing unit's own dest: {alone.carry_from}"
    )

    (together,) = adapter.workspace_files([first, second])
    assert together.path == alone.path, "still exactly one root lock, at one path"
    assert together.carry_from == [], (
        "two contributors: no single repo's lock is a resolution of the union, and a carry "
        "candidate here skips the resolver that would produce one"
    )
    # The floor unions too — a floor holding one unit's specs is the same silent drop one step
    # further down, and it is the text `resolution()` hands the resolver as `requirements.in`.
    assert "requests>=2.31" in together.content, together.content
    assert "httpx>=0.27" in together.content, together.content
    reverse = adapter.workspace_files([second, first])
    assert [f.model_dump(mode="json") for f in reverse] == [
        together.model_dump(mode="json")
    ], "the root lock is a function of the fleet, not of the order the driver grouped it in"

    # A unit that declares nothing is not a contributor: it must neither create a root lock on
    # its own nor revoke the carry for the one repo that has dependencies.
    bare = _unit("acme-docs", Ecosystem.PYPI, "py/acme-docs", srcs=["acme_docs/__init__.py"])
    assert adapter.workspace_files([bare]) == []
    (with_bare,) = adapter.workspace_files([first, bare])
    assert with_bare.model_dump(mode="json") == alone.model_dump(mode="json")


def test_py_contradictory_specs_from_two_units_reach_the_resolver_unmerged() -> None:
    """Two units pinning the SAME distribution to incompatible ranges put BOTH specs into
    `requirements.in` — the conflict reaches `uv`, which fails loudly, instead of being averaged.

    **Why (Rule 11):** the monorepo's single `@pypi` hub cannot hold two versions of one
    distribution, so two repos that pinned `urllib3<2` and `urllib3>=2.2` are a genuine conflict
    that the migration must surface rather than settle. The dangerous shape is a name-keyed dict:
    `{c.name: spec}` unioned over the fleet overwrites in place, emits one line, and produces a
    lock in which one repo silently builds against a version it explicitly excluded. The set of
    full spec strings keeps both lines, and real `uv pip compile` answers with `No solution found
    when resolving dependencies … unsatisfiable`, exit 1 — which `cli._run_resolution` reports as
    a `DependencyResolutionError` naming the command and its stderr.

    Asserted on the resolver's INPUT, offline, because the input is the half this harness owns;
    that a contradictory input really fails is `uv`'s own behaviour, exercised for real by the
    integration suite.
    """
    adapter = ecosystems.for_ecosystem(Ecosystem.PYPI)
    old = _unit(
        "acme-legacy",
        Ecosystem.PYPI,
        "py/acme-legacy",
        srcs=["acme_legacy/__init__.py"],
        external=[Coordinate(ecosystem=Ecosystem.PYPI, name="urllib3", version_spec="<2")],
    )
    new = _unit(
        "acme-modern",
        Ecosystem.PYPI,
        "py/acme-modern",
        srcs=["acme_modern/__init__.py"],
        external=[Coordinate(ecosystem=Ecosystem.PYPI, name="urllib3", version_spec=">=2.2")],
    )

    plan = adapter.resolution([old, new])
    assert plan is not None
    (source,) = plan.inputs
    specs = [line for line in source.content.splitlines() if line.startswith("urllib3")]
    assert sorted(specs) == ["urllib3<2", "urllib3>=2.2"], (
        f"one of the two pins was dropped before the resolver ever saw the conflict: {specs}"
    )
    # …and neither is the root lock's floor a silent winner: the same two lines survive there.
    (lock,) = adapter.workspace_files([old, new])
    assert "urllib3<2" in lock.content and "urllib3>=2.2" in lock.content, lock.content


def _rust_unit(unit_id: str, dest: str, dep: str) -> BuildUnit:
    """A Rust crate with one crates.io dependency of its own — the fixture the tests below need."""
    return _unit(
        unit_id,
        Ecosystem.CARGO,
        dest,
        srcs=["src/lib.rs"],
        published=Coordinate(ecosystem=Ecosystem.CARGO, name=dest.rsplit("/", maxsplit=1)[-1]),
        external=[Coordinate(ecosystem=Ecosystem.CARGO, name=dep, version_spec="1.0")],
    )


def test_rust_root_manifest_is_a_workspace_naming_every_contributing_crate() -> None:
    """Two Rust repos, ONE root `Cargo.toml`, and `members` holds BOTH of their dests.

    **Why:** `_workspace_manifest` used to be rendered per unit — `members = ["<that one dest>"]` —
    and `union_workspace_files`' first-writer-wins `setdefault` on `path` then threw every later
    unit's copy away. Two Rust repos therefore produced a workspace containing one of them, chosen
    by nothing more meaningful than the lower `(dest, unit_id)`, and the second crate was absent
    from the manifest `crate.from_cargo(manifests = ["//:Cargo.toml"])` splices. Nothing could
    raise on it: `cli._fleet_support_files` computes the root files once per ecosystem and hands
    every plan the identical tuple, so `RootFileConflictError` never sees two candidates at all —
    the union had nothing to reject, and the drop was invisible from the driver down.

    The membership gate is the SAME one the tags are gated on (`external_coordinates`), so a repo
    that names no crates.io dependency is not a member and does not create a workspace on its own.
    """
    adapter = ecosystems.for_ecosystem(Ecosystem.CARGO)
    store = _rust_unit("acme-store", "rust/acme-store", "serde")
    core = _rust_unit("acme-core", "rust/acme-core", "anyhow")

    files = {support.path: support for support in adapter.workspace_files([store, core])}
    assert set(files) == {"Cargo.lock", "Cargo.toml"}
    manifest = files["Cargo.toml"].content
    assert 'members = ["rust/acme-core", "rust/acme-store"]' in manifest, manifest
    assert "[workspace]" in manifest and 'resolver = "2"' in manifest, manifest

    # The one-unit view names one member and is otherwise the same file — widening the arity must
    # not add or drop a root path.
    (lock_alone, manifest_alone) = adapter.workspace_files([store])
    assert (lock_alone.path, manifest_alone.path) == ("Cargo.lock", "Cargo.toml")
    assert 'members = ["rust/acme-store"]' in manifest_alone.content

    # A crate with no external coordinate is not a contributor: it declares no `crate.from_cargo`
    # tag, so it must neither create root files on its own nor appear in the workspace another
    # repo's tag names.
    bare = _unit("acme-docs", Ecosystem.CARGO, "rust/acme-docs", srcs=["src/lib.rs"])
    assert adapter.workspace_files([bare]) == []
    assert adapter.workspace_deps(bare) == []
    with_bare = {f.path: f for f in adapter.workspace_files([store, bare])}
    assert with_bare["Cargo.toml"].content == manifest_alone.content


def test_rust_workspace_files_hold_one_entry_per_root_path_and_ignore_unit_order() -> None:
    """One `SupportFile` per monorepo-root path however many Rust repos name it, and identical
    bytes whichever order the driver grouped the units in.

    **Why (§11.6):** `//:Cargo.lock` and `//:Cargo.toml` are files at the monorepo ROOT, so the
    fleet holds exactly one of each; two candidates for one path is a root file that was computed
    per repo while describing the whole fleet, and `cli._module_inputs` would keep whichever it
    saw first. Order-independence matters because these bytes are committed onto the integration
    branch and are an input to the run-equivalence digest — a `members` list taken by iteration
    would make two runs of the same fleet differ with no diff anywhere in this repository.
    """
    adapter = ecosystems.for_ecosystem(Ecosystem.CARGO)
    store = _rust_unit("acme-store", "rust/acme-store", "serde")
    core = _rust_unit("acme-core", "rust/acme-core", "anyhow")

    forward = adapter.workspace_files([store, core])
    paths = [support.path for support in forward]
    assert sorted(paths) == sorted(set(paths)), f"a root path appears twice: {paths}"
    reverse = adapter.workspace_files([core, store])
    assert [support.model_dump(mode="json") for support in forward] == [
        support.model_dump(mode="json") for support in reverse
    ]


def test_rust_root_manifest_drops_its_carry_while_the_lock_keeps_one() -> None:
    """`//:Cargo.toml` declares NO `carry_from`; `//:Cargo.lock` keeps its, from a member's dest.

    **Why the manifest drops it.** A repo's own `Cargo.toml` is a `[package]` manifest with no
    `[workspace]` table, and crate_universe's `SplicerKind::new` dispatches on precisely that:
    handed one it takes the `Package` branch rather than the `Workspace` branch, so a single
    crate's dependencies reach the `@crates` hub and every other member's are missing. It is
    `js.py`'s `pnpm-workspace.yaml` argument in Cargo's dialect — a workspace manifest describes a
    workspace no source repo has ever seen, so no source repo's copy can be promoted to the root.

    **Why the lock does NOT follow `py.py`'s ≥2-contributors rule (ADR-0049 is Python-specific).**
    `crate_universe`'s `LockGenerator::generate` runs `cargo fetch` **without** `--locked` when a
    lock exists, so cargo repairs and extends a partial lock: one repo's carried pins survive and
    the other repo's crates are added beside them. `pip.parse` has no downstream resolver to do
    that, which is why dropping the carry is right there and lossy here — the alternative is the
    `version = 3` floor, which throws every pin the fleet had away.

    The carry candidate is asserted to live under a contributing unit's own `dest`, because that —
    not the file's name — is what makes it a repo's lock rather than one the harness wrote itself.
    """
    adapter = ecosystems.for_ecosystem(Ecosystem.CARGO)
    store = _rust_unit("acme-store", "rust/acme-store", "serde")
    core = _rust_unit("acme-core", "rust/acme-core", "anyhow")

    files = {support.path: support for support in adapter.workspace_files([store, core])}
    assert files["Cargo.toml"].carry_from == [], (
        "a [package] manifest carried to the root sends crate_universe's splicer down the "
        "Package branch, and only one crate's dependencies reach @crates"
    )
    assert files["Cargo.lock"].carry_from == ["rust/acme-core/Cargo.lock"], (
        files["Cargo.lock"].carry_from
    )
    assert all(
        candidate.startswith(("rust/acme-core/", "rust/acme-store/"))
        for candidate in files["Cargo.lock"].carry_from
    ), files["Cargo.lock"].carry_from


def test_rust_every_workspace_member_has_a_manifest_the_adapter_declares() -> None:
    """Each `members` entry names a directory whose `Cargo.toml` is a file this adapter writes.

    **Why:** `members = ["rust/acme-store"]` is a promise that `rust/acme-store/Cargo.toml` loads,
    and `cargo metadata` fails the WHOLE workspace — not just that member — when it does not. So
    unioning the members without declaring their manifests would trade the silent drop above for a
    hard failure. The member file is carried from itself, exactly as `js.py`'s `tsconfig.json` is:
    relocation already put a repo's own manifest at that path, so materializing it is a no-op, and
    `content` is the floor for a repo that shipped none — a `[package]`, never a second
    `[workspace]`, which cargo rejects as a nested workspace.
    """
    import re

    adapter = ecosystems.for_ecosystem(Ecosystem.CARGO)
    store = _rust_unit("acme-store", "rust/acme-store", "serde")
    core = _rust_unit("acme-core", "rust/acme-core", "anyhow")

    manifest = next(
        f for f in adapter.workspace_files([store, core]) if f.path == "Cargo.toml"
    ).content
    members = re.findall(r'"([^"]+)"', manifest.split("members = [", 1)[1].split("]", 1)[0])
    declared = {f.path for unit in (store, core) for f in adapter.package_files(unit)}
    assert {f"{member}/Cargo.toml" for member in members} == declared, (declared, members)

    (member_file,) = adapter.package_files(store)
    assert member_file.carry_from == [member_file.path], "the repo's own manifest is the carry"
    assert "[package]" in member_file.content and "[workspace]" not in member_file.content
    assert 'name = "acme_store"' in member_file.content, member_file.content
    assert 'edition = "2021"' in member_file.content, member_file.content

    # A non-contributor is not a member, so it gets no floor manifest either: the two sets are the
    # same set by construction rather than by coincidence.
    bare = _unit("acme-docs", Ecosystem.CARGO, "rust/acme-docs", srcs=["src/lib.rs"])
    assert adapter.package_files(bare) == []


# =======================================================================================
# The §13 invariant: per-language knowledge lives ONLY in the adapter packages
# =======================================================================================


def _python_sources() -> list[Path]:
    return sorted(p for p in SRC_ROOT.rglob("*.py"))


def _outside_the_adapter_packages(path: Path) -> bool:
    relative = path.relative_to(REPO_ROOT).as_posix()
    return not any(relative.startswith(pkg) for pkg in ADAPTER_PACKAGES)


def test_no_ecosystem_branch_exists_outside_the_adapter_packages() -> None:
    """§12.6(b), the branching half, asserted with no allowance whatsoever.

    **Why:** the entire justification for this package is that adding a language is a file drop.
    One `if unit.ecosystem == Ecosystem.NPM` in a driver moves a piece of that knowledge
    somewhere the next language's author will not think to look, and the claim quietly stops
    being true while every test still passes.
    """
    import re

    pattern = re.compile(r"if +(unit\.|repo\.|coordinate\.)?ecosystem *(==|!=|is)")
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{n}: {line.strip()}"
        for path in _python_sources()
        if _outside_the_adapter_packages(path)
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if pattern.search(line)
    ]
    assert offenders == []


def test_no_ecosystem_member_other_than_the_unknown_sentinel_is_named_outside_the_packages(
) -> None:
    """§12.6(b)'s second half, plus the one documented residue.

    `Ecosystem.UNKNOWN` still appears in `graph/infer.py`, `workers/` and `cli.py` — always as
    the *value* assigned when no ecosystem could be determined (§3.1 step 2), never in a
    comparison. That is not language knowledge: "no language" is the one member every layer is
    entitled to name, and the branching half above proves none of those lines is a branch.
    Naming any *other* member outside the two adapter packages is language knowledge and fails.

    **Why:** an allowlist of file:line would rot on the next edit and be widened without anyone
    noticing; a structural rule cannot be widened silently.
    """
    import re

    pattern = re.compile(r"Ecosystem\.([A-Z_]+)")
    offenders: list[str] = []
    for path in _python_sources():
        if not _outside_the_adapter_packages(path):
            continue
        if path.relative_to(REPO_ROOT).as_posix() == "src/fleet/models/enums.py":
            continue  # where the enum is defined
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            named = {m.group(1) for m in pattern.finditer(line)}
            if named - {"UNKNOWN"}:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{n}: {line.strip()}")
    assert offenders == []


def test_the_exemption_list_is_exactly_the_two_adapter_packages() -> None:
    """§12.6(a): "the exemption list is exactly two package paths and is asserted to be exactly
    two, so widening it is a visible diff".

    **Why:** every other assertion in this section is parameterized by this tuple. Without this,
    the cheapest way to make a failing invariant test pass is to add a third path to it.
    """
    assert ADAPTER_PACKAGES == ("src/fleet/manifests/", "src/fleet/ecosystems/")
    assert all((REPO_ROOT / pkg).is_dir() for pkg in ADAPTER_PACKAGES)


def test_no_language_directory_is_hardcoded_in_any_driver() -> None:
    """§13 row 33's grep: `'"(java|ts|py|go|rust|misc)/'` over `bazel/`, `orchestrator/`,
    `graph/` returns nothing.

    **Why:** the directory names are `adapter.monorepo_dir`, and a driver that spells one of them
    is a driver that keeps its own copy — so re-pointing an adapter moves *most* of the tree and
    leaves a few labels behind, which is worse than not moving it at all.
    """
    import re

    pattern = re.compile(r'"(java|ts|py|go|rust|misc)/')
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{n}: {line.strip()}"
        for driver in ("bazel", "orchestrator", "graph")
        for path in sorted((SRC_ROOT / "fleet" / driver).rglob("*.py"))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if pattern.search(line)
    ]
    assert offenders == []


def test_an_adapters_unrenderable_coordinate_error_is_the_neutral_one_the_driver_can_catch(
) -> None:
    """`GoModuleCoordinateError` IS an `ecosystems.AdapterCoordinateError`, and that lineage is
    what lets §3.3 step 2 contain it without knowing an ecosystem exists.

    **Why this is an assertion and not an implementation detail.** The Go error was a bare
    `ValueError` and the driver's per-ecosystem containment catches what the driver knows about,
    so it escaped: one coordinate that could not be written as a `require` line ended the entire
    fleet's build instead of that ecosystem's repos. The fix cannot be "the driver catches
    `GoModuleCoordinateError`" — that is language knowledge in a driver (§12.6) — so it has to be
    a neutral base the adapters raise and the driver names. Nothing else enforces that: an adapter
    author writing the next `class FooCoordinateError(ValueError)` reintroduces exactly the
    original defect, silently, and every existing test still passes.

    `ValueError` is asserted too, because the Go class shipped as one and dropping it from the
    lineage would break any caller catching it — including the tests above.
    """
    assert issubclass(go_adapter.GoModuleCoordinateError, ecosystems.AdapterCoordinateError)
    assert issubclass(ecosystems.AdapterCoordinateError, ValueError)


def test_no_adapter_package_exception_is_named_outside_the_adapter_packages() -> None:
    """§12.6(b), extended to the one form of language knowledge the greps above cannot see: an
    exception class that only one adapter defines, named by a driver that catches it.

    **Why a new invariant.** `test_no_ecosystem_branch_exists_outside_the_adapter_packages` greps
    for `if …ecosystem ==` and its sibling for `Ecosystem.<MEMBER>`; an
    `except GoModuleCoordinateError` in `cli.py` matches neither, and it is language knowledge in
    a driver just as surely — re-pointing it at another adapter's error is a second `except`
    clause the next language's author will not think to add. The rule is the same one those two
    tests use: the exception types shared across drivers live in each package's `base.py`, and
    everything else is per-language and stays inside the package.

    The scan is over class names ending in `Error` declared anywhere in the two adapter packages
    outside their `base.py`, so it needs no allowlist and cannot be widened without moving the
    class — which is the point of asserting it structurally rather than by file:line.
    """
    import ast

    owned: dict[str, str] = {}
    for package in ADAPTER_PACKAGES:
        for path in sorted((REPO_ROOT / package).rglob("*.py")):
            if path.name in {"base.py", "__init__.py"}:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ClassDef) and node.name.endswith("Error"):
                    owned[node.name] = path.relative_to(REPO_ROOT).as_posix()
    assert "GoModuleCoordinateError" in owned, (
        f"the scan found no per-adapter exception at all, so it proves nothing: {owned}"
    )

    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{n}: {line.strip()} (defined in {owned[name]})"
        for path in _python_sources()
        if _outside_the_adapter_packages(path)
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        for name in owned
        if name in line
    ]
    assert offenders == []


# =======================================================================================
# §12.6(a) as SPEC.md literally words it: an AST walk over Compare/Subscript/match, not a
# sixth regex — the ContractKind half the five tests above never covered, plus the match/case
# half no test covered at all (ADR-0100)
# =======================================================================================


def _module_scope_table_names(tree: object) -> set[str]:
    """Names bound, at module scope, directly to a `{...}` dict literal.

    ADR-0100's scoped-subscript reading: `SYMBOL_IDENTIFIED[ContractKind.PROTO]` is the
    compliant "table, not branch" pattern §1 mandates, not an instance of the branch it exists
    to replace, exactly when `SYMBOL_IDENTIFIED` is a name bound like this — a dict literal,
    assigned at module scope, in the same file. A name imported from elsewhere, built by a call
    (`dict(...)`) or comprehension, or bound inside a function does not qualify: the ruling
    scopes the exemption to that one shape, not to every subscript whose base merely looks
    table-shaped.
    """
    import ast

    assert isinstance(tree, ast.Module)
    names: set[str] = set()
    for node in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and isinstance(value, ast.Dict):
            names.add(target.id)
    return names


def _kind_member_name(node: object) -> str | None:
    """`ContractKind.X` / `Ecosystem.X` → `"ContractKind.X"`; a `Tuple`/`List`/`Set` literal
    containing one (e.g. `kind in (ContractKind.X, ContractKind.Y)`) → the first such member's
    name; anything else → `None`."""
    import ast

    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in ("ContractKind", "Ecosystem")
    ):
        return f"{node.value.id}.{node.attr}"
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        for element in node.elts:
            name = _kind_member_name(element)
            if name is not None:
                return name
    return None


def _confined_modules_asts() -> Iterator[tuple[str, object]]:
    """Every `_python_sources()` module outside the adapter packages and the enum's own
    definition site, parsed once. Shared by both AST-walk tests below."""
    import ast

    for path in _python_sources():
        if not _outside_the_adapter_packages(path):
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel == "src/fleet/models/enums.py":
            continue
        yield rel, ast.parse(path.read_text(encoding="utf-8"), filename=rel)


def test_no_bare_compare_or_subscript_names_a_kind_member_outside_the_adapter_packages() -> None:
    """§12.6(a), as an AST walk rather than a line scan: no `Compare` or `Subscript` node outside
    the two adapter packages may name an `Ecosystem` or `ContractKind` member — except a
    `Subscript` whose base is a module-scope dict-literal table in the same file (ADR-0100).

    **Why this test exists beside the five above.** Those five only ever scanned for
    `Ecosystem` — `ContractKind` had no equivalent gate at all, and
    `src/fleet/workers/contracts.py:758`'s `if kind is ContractKind.OPENAPI:` sat there,
    contradicting the module's own "there is no `if kind is …`" docstring claim, undetected by
    any test in this file (round Q, ADR-0100). A regex could be widened to catch that one
    `Compare`, but it cannot make the scoped-subscript judgment ADR-0100 requires:
    distinguishing `SYMBOL_IDENTIFIED[ContractKind.PROTO]` (a table read, permitted) from a
    hypothetical subscript on a name that is NOT a module-scope table in the same file (a
    branch wearing a subscript's clothes, forbidden) needs to know what the base name is bound
    to — a question only the AST can answer, which is why this is a walk and not a sixth regex.
    """
    import ast

    offenders: list[str] = []
    for rel, tree in _confined_modules_asts():
        assert isinstance(tree, ast.Module)
        tables = _module_scope_table_names(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                for operand in (node.left, *node.comparators):
                    name = _kind_member_name(operand)
                    if name is not None:
                        offenders.append(
                            f"{rel}:{node.lineno}: Compare against {name} — {ast.unparse(node)}"
                        )
            elif isinstance(node, ast.Subscript):
                name = _kind_member_name(node.slice)
                if name is None:
                    continue
                base_is_local_table = isinstance(node.value, ast.Name) and node.value.id in tables
                if not base_is_local_table:
                    offenders.append(
                        f"{rel}:{node.lineno}: Subscript keyed by {name} — {ast.unparse(node)}"
                    )
    assert offenders == []


def test_no_match_case_names_a_kind_member_outside_the_adapter_packages() -> None:
    """§12.6(a)'s `match` half. No production code touches either enum through `match`/`case`
    today — this test is deliberately vacuous on `main`, the same "gap that would pass every
    existing test" shape the four tests above guard against elsewhere in this file — so its only
    job is to fail the moment someone writes one, per-kind branching's second syntax after `if`.

    A `case Ecosystem.PYTHON:` line has no `if`/`==`/`is`-adjacent shape any of the five
    line-scanning tests above look for, so line-scanning cannot reliably catch it; this is an AST
    walk, matching `tests/test_instruments_are_armed.py`'s style for this codebase's structural
    tests. The whole `match` statement's subtree (subject AND every `case` pattern) is scanned,
    not just the subject expression: the mutation this test exists to catch —
    `match ecosystem: case Ecosystem.PYTHON:` — names the member in the *pattern*, not the
    subject, which is a bare local variable.
    """
    import ast

    offenders: list[str] = []
    for rel, tree in _confined_modules_asts():
        assert isinstance(tree, ast.Module)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Match):
                continue
            for sub in ast.walk(node):
                name = _kind_member_name(sub)
                if name is not None:
                    offenders.append(
                        f"{rel}:{node.lineno}: match/case names {name} — {ast.unparse(node)}"
                    )
                    break
    assert offenders == []


# =======================================================================================
# Determinism across processes
# =======================================================================================

_DIGEST_SCRIPT = """
import json
from fleet import ecosystems
from fleet.models.build import BuildUnit, InternalDep
from fleet.models.enums import Ecosystem
from fleet.models.repo import Coordinate

registry = ecosystems.discover()
unit = BuildUnit(
    unit_id="acme-ui",
    ecosystem=Ecosystem.NPM,
    dest="ts/acme/ui",
    srcs=["src/index.ts", "src/Button.tsx"],
    test_srcs=["src/Button.spec.ts"],
    published=Coordinate(ecosystem=Ecosystem.NPM, group="@acme", name="ui"),
    internal_deps=[
        InternalDep(
            label="//ts/acme/tokens:tokens",
            dest="ts/acme/tokens",
            published=Coordinate(ecosystem=Ecosystem.NPM, group="@acme", name="tokens"),
        )
    ],
    external_coordinates=[
        Coordinate(ecosystem=Ecosystem.NPM, name="react"),
        Coordinate(ecosystem=Ecosystem.NPM, group="@acme", name="icons"),
    ],
)
adapter = ecosystems.for_ecosystem(Ecosystem.NPM)
print(json.dumps({
    "order": [a.name for a in ecosystems.adapters()],
    "dirs": {e.value: d for e, d in sorted(ecosystems.monorepo_dirs().items())},
    "rules": {e.value: r for e, r in sorted(ecosystems.library_rules().items())},
    "tails": {
        e.value: registry[e].path_tail(Coordinate(ecosystem=e, group="com.acme", name="Widget"))
        for e in sorted(Ecosystem)
    },
    "targets": [t.model_dump(mode="json") for t in adapter.generate_targets(unit)],
    "specifiers": {
        e.value: registry[e].import_specifier(
            Coordinate(ecosystem=e, group="com.acme", name="Widget"), "misc/widget"
        )
        for e in sorted(Ecosystem)
    },
    "deps": [d.model_dump(mode="json") for d in adapter.workspace_deps(unit)],
}, sort_keys=True))
"""


def _digest(seed: str) -> str:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _DIGEST_SCRIPT],
        cwd=REPO_ROOT,
        env={"PYTHONHASHSEED": seed, "PYTHONPATH": str(SRC_ROOT), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_the_registry_and_its_output_are_identical_across_processes() -> None:
    """Two fresh interpreters, two different `PYTHONHASHSEED`s, byte-identical output.

    **Why (§11.6):** `frozenset` iteration, `set` unions and `dict` insertion order all move
    under a different hash seed, and adapters use all three. If any of that reached a `dest`, a
    target list or a `WorkspaceDep`, the same fleet would regenerate every `BUILD.bazel` on the
    next run — an all-files diff on the integration branch that hides the real change and makes
    §12.21's run-equivalence digest compare two hash seeds instead of two runs.
    """
    first = _digest("0")
    second = _digest("1")
    assert first == second
    parsed = json.loads(first)
    assert parsed["order"] == ["go", "js", "jvm", "py", "rust", "unknown"]
    assert parsed["dirs"]["npm"] == "ts"
