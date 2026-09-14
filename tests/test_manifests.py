"""`ManifestAdapter` registry + per-ecosystem adapters (SPEC §7.3, §3.1 steps 2-3, ADR-0005).

Every test here states *why* the behaviour matters, because most of these properties fail
silently if they regress: a non-total tie-break, a stateful singleton and a parser that returns
`[]` on garbage all produce a graph that is merely *wrong*, not a run that is *red*.

Fixtures are written into `tmp_path` and are deliberately realistic — a `pom.xml` with a
`<parent>` and a property reference, a `Cargo.toml` with a workspace, a `go.mod` with a
`require` block — because a one-line toy proves nothing about the forms real repos ship.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest

from fleet.manifests import base
from fleet.manifests.base import (
    ManifestAdapter,
    ManifestParseError,
    adapter_for,
    adapters,
    discover,
    register,
)
from fleet.manifests.cargo import CargoAdapter
from fleet.manifests.gomod import GomodAdapter
from fleet.manifests.gradle import GradleAdapter
from fleet.manifests.maven import MavenAdapter
from fleet.manifests.npm import NpmAdapter
from fleet.manifests.python import PythonAdapter
from fleet.manifests.unknown import UnknownAdapter
from fleet.models.enums import Ecosystem
from fleet.models.repo import RawDependency

SRC = Path(__file__).resolve().parents[1] / "src"


def write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


def test_shared_helpers_common_shape_contract(tmp_path: Path) -> None:
    """`as_table`/`as_str`/`opt_str` are the ONE place every adapter narrows an untyped TOML/JSON
    node into the common shape (base.py's own docstring: "the common-shape contract"). `opt_str`
    in particular backs every optional string field across all 7 ecosystems (`version`, `name`,
    catalog aliases) — a field that is present but whitespace-only must count as absent, or a
    manifest that writes `version = "   "` would put a fake non-None `version_spec` in the graph
    instead of `None`."""
    path = tmp_path / "x.toml"
    assert base.as_table({"a": 1}, path=path, what="t") == {"a": 1}
    with pytest.raises(ManifestParseError, match=r"x\.toml: expected a table for t, got list"):
        base.as_table([1, 2], path=path, what="t")

    assert base.as_str("hi", path=path, what="s") == "hi"
    with pytest.raises(ManifestParseError, match=r"x\.toml: expected a string for s, got int"):
        base.as_str(3, path=path, what="s")

    assert base.opt_str("  hi  ") == "hi"
    assert base.opt_str("   ") is None  # whitespace-only counts as absent, not a value
    assert base.opt_str(None) is None
    assert base.opt_str(3) is None


@pytest.fixture
def isolated_registry() -> Iterator[None]:
    """Save/restore the module-global registry so a test may register throwaway adapters."""
    saved = base.adapters()
    saved_discovered = base._DISCOVERED
    try:
        yield
    finally:
        base.reset_adapters()
        base._ADAPTERS.extend(saved)
        base._DISCOVERED = saved_discovered


# --------------------------------------------------------------------------------------
# npm
# --------------------------------------------------------------------------------------

PACKAGE_JSON = """
    {
      "name": "@acme/checkout-ui",
      "version": "3.1.0",
      "private": false,
      "workspaces": ["packages/*"],
      "scripts": { "build": "tsc -b" },
      "dependencies": {
        "react": "^18.2.0",
        "@acme/design-tokens": "workspace:*",
        "lodash.merge": "4.6.2"
      },
      "devDependencies": {
        "typescript": "~5.4.0",
        "vitest": "^1.6.0"
      },
      "peerDependencies": { "react-dom": ">=18" },
      "optionalDependencies": { "fsevents": "^2.3.3" }
    }
"""


def test_npm_parses_every_dependency_section(tmp_path: Path) -> None:
    """dev/peer/optional deps are real graph edges: a repo that only *test*-depends on a fleet
    sibling still cannot be migrated before it, so dropping devDependencies under-orders waves."""
    path = write(tmp_path, "package.json", PACKAGE_JSON)
    deps = NpmAdapter().parse(path)

    assert [(d.raw_id, d.scope) for d in deps] == [
        ("@acme/design-tokens", "prod"),
        ("lodash.merge", "prod"),
        ("react", "prod"),
        ("typescript", "dev"),
        ("vitest", "dev"),
        ("react-dom", "peer"),
        ("fsevents", "optional"),
    ]
    assert next(d for d in deps if d.raw_id == "react").version_spec == "^18.2.0"
    assert next(d for d in deps if d.raw_id == "fsevents").optional is True


def test_npm_scope_becomes_the_coordinate_group(tmp_path: Path) -> None:
    """`@acme/ui` and `ui` are different packages; folding the scope away would join two
    unrelated repos onto one `Coordinate.key` and fabricate an edge."""
    adapter = NpmAdapter()
    scoped = adapter.coordinate(RawDependency(raw_id="@acme/design-tokens", version_spec="^1.0"))
    bare = adapter.coordinate(RawDependency(raw_id="react"))

    assert (scoped.group, scoped.name) == ("@acme", "design-tokens")
    assert scoped.key == "npm:@acme:design-tokens"
    assert (bare.group, bare.name, bare.key) == ("", "react", "npm::react")

    published = adapter.publishes(write(tmp_path, "package.json", PACKAGE_JSON))
    assert published is not None
    assert (published.key, published.version_spec) == ("npm:@acme:checkout-ui", "3.1.0")


def test_npm_empty_package_name_fails_loud(tmp_path: Path) -> None:
    """An empty string key in a dependency table is not a package — a corrupted/hand-edited
    package.json with `"": "1.0.0"` must fail loud (Rule 11), not silently become a `Coordinate`
    with an empty name that every other empty-name entry in the fleet then collides with."""
    path = write(tmp_path, "package.json", '{"dependencies": {"": "1.0.0"}}')
    with pytest.raises(ManifestParseError, match=r"empty package name"):
        NpmAdapter().parse(path)


# --------------------------------------------------------------------------------------
# python
# --------------------------------------------------------------------------------------

PYPROJECT = """
    [build-system]
    requires = ["hatchling>=1.25"]
    build-backend = "hatchling.build"

    [project]
    name = "Acme_Billing.Service"
    version = "2.0.1"
    requires-python = ">=3.12"
    dependencies = [
      "pydantic>=2.11,<3",
      "httpx[http2]>=0.27",
      "Flask_SQLAlchemy==3.1.1",
      "acme-shared @ git+https://git.example.com/acme/shared.git@v1.2.0",
    ]

    [project.optional-dependencies]
    otel = ["opentelemetry-sdk>=1.27"]

    [dependency-groups]
    dev = ["pytest>=8.3", "mypy>=1.13"]
"""


def test_python_pyproject_pep621_and_pep735(tmp_path: Path) -> None:
    """Extras and PEP 735 groups carry the scope they were declared under, and PEP 503
    normalization is applied at `coordinate()` — `Flask_SQLAlchemy` and `flask-sqlalchemy` are
    one package, and two repos naming it differently must land on one node."""
    adapter = PythonAdapter()
    path = write(tmp_path, "pyproject.toml", PYPROJECT)
    deps = adapter.parse(path)

    assert [(d.raw_id, d.scope) for d in deps] == [
        ("pydantic", "main"),
        ("httpx", "main"),
        ("Flask_SQLAlchemy", "main"),
        ("acme-shared", "main"),
        ("opentelemetry-sdk", "otel"),
        ("pytest", "dev"),
        ("mypy", "dev"),
    ]
    assert next(d for d in deps if d.raw_id == "pydantic").version_spec == ">=2.11,<3"
    assert next(d for d in deps if d.raw_id == "opentelemetry-sdk").optional is True
    assert adapter.coordinate(deps[2]).key == "pypi::flask-sqlalchemy"

    published = adapter.publishes(path)
    assert published is not None
    assert published.key == "pypi::acme-billing-service"


def test_python_requirements_txt_keeps_line_numbers(tmp_path: Path) -> None:
    """`source_line` is the operator's only way back to the declaration when a resolved edge
    looks wrong; pip options and comments are configuration, not requirements."""
    path = write(
        tmp_path,
        "requirements-dev.txt",
        """
        # pinned by the platform team
        --index-url https://pypi.example.com/simple
        -r requirements.txt

        requests==2.32.3
        boto3>=1.35  # aws
        uvicorn[standard]~=0.30
        """,
    )
    deps = PythonAdapter().parse(path)

    assert [(d.raw_id, d.version_spec, d.source_line) for d in deps] == [
        ("requests", "==2.32.3", 5),
        ("boto3", ">=1.35", 6),
        ("uvicorn", "~=0.30", 7),
    ]


def test_python_setup_cfg(tmp_path: Path) -> None:
    """setup.cfg is still the manifest of choice in older services; ignoring it would make a
    whole class of repos look dependency-free."""
    path = write(
        tmp_path,
        "setup.cfg",
        """
        [metadata]
        name = acme-legacy
        version = 0.9.0

        [options]
        install_requires =
            requests>=2.28
            sqlalchemy<2

        [options.extras_require]
        test =
            pytest
        """,
    )
    deps = PythonAdapter().parse(path)
    assert [(d.raw_id, d.scope) for d in deps] == [
        ("requests", "main"),
        ("sqlalchemy", "main"),
        ("pytest", "test"),
    ]
    published = PythonAdapter().publishes(path)
    assert published is not None and published.key == "pypi::acme-legacy"


def test_python_dependency_group_include_group_table_entries_are_skipped(tmp_path: Path) -> None:
    """PEP 735 `dependency-groups` entries may be a table (`{include-group = "other"}`) — an
    indirection into another group in the SAME file, not an external requirement string. Trying
    to `split_requirement` a table would crash the whole parse for a syntactically valid
    pyproject.toml, dropping every dependency of that repo, not just the indirection."""
    path = write(
        tmp_path,
        "pyproject.toml",
        """
        [dependency-groups]
        test = ["pytest>=8.3", {include-group = "typing"}]
        typing = ["mypy>=1.13"]
        """,
    )
    deps = PythonAdapter().parse(path)
    assert [(d.raw_id, d.scope) for d in deps] == [
        ("pytest", "test"),
        ("mypy", "typing"),
    ]


# --------------------------------------------------------------------------------------
# maven
# --------------------------------------------------------------------------------------

POM = """
    <?xml version="1.0" encoding="UTF-8"?>
    <project xmlns="http://maven.apache.org/POM/4.0.0">
      <modelVersion>4.0.0</modelVersion>
      <parent>
        <groupId>com.acme</groupId>
        <artifactId>acme-parent</artifactId>
        <version>5.2.0</version>
      </parent>
      <artifactId>billing-service</artifactId>
      <properties>
        <jackson.version>2.17.1</jackson.version>
      </properties>
      <dependencyManagement>
        <dependencies>
          <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-dependencies</artifactId>
            <version>3.3.0</version>
            <type>pom</type>
            <scope>import</scope>
          </dependency>
        </dependencies>
      </dependencyManagement>
      <dependencies>
        <dependency>
          <groupId>com.acme</groupId>
          <artifactId>acme-commons</artifactId>
          <version>${project.version}</version>
        </dependency>
        <dependency>
          <groupId>com.fasterxml.jackson.core</groupId>
          <artifactId>jackson-databind</artifactId>
          <version>${jackson.version}</version>
        </dependency>
        <dependency>
          <groupId>org.junit.jupiter</groupId>
          <artifactId>junit-jupiter</artifactId>
          <version>5.10.2</version>
          <scope>test</scope>
          <optional>true</optional>
        </dependency>
      </dependencies>
    </project>
"""


def test_maven_parent_dependencies_and_properties(tmp_path: Path) -> None:
    """A `<parent>` is an ordering constraint as strong as a compile dep, and `${jackson.version}`
    must resolve from this pom's own `<properties>` — an unresolved placeholder written into
    `version_spec` is noise the operator has to decode by hand."""
    adapter = MavenAdapter()
    path = write(tmp_path, "pom.xml", POM)
    deps = adapter.parse(path)

    assert [(d.raw_id, d.scope) for d in deps] == [
        ("com.acme:acme-parent:5.2.0", "parent"),
        ("com.acme:acme-commons:5.2.0", "compile"),
        ("com.fasterxml.jackson.core:jackson-databind:2.17.1", "compile"),
        ("org.junit.jupiter:junit-jupiter:5.10.2", "test"),
        ("org.springframework.boot:spring-boot-dependencies:3.3.0", "import"),
    ]
    assert next(d for d in deps if d.scope == "test").optional is True
    assert adapter.coordinate(deps[0]).key == "maven:com.acme:acme-parent"

    published = adapter.publishes(path)
    assert published is not None
    # groupId and version are omitted here and inherited from <parent>, per the POM 4.0.0 model.
    assert (published.key, published.version_spec) == ("maven:com.acme:billing-service", "5.2.0")


def test_maven_rejects_entity_expansion(tmp_path: Path) -> None:
    """Cloned third-party poms are untrusted input; expat expands internal entity bombs. A
    DOCTYPE is refused loudly rather than parsed, and no real pom declares one."""
    path = write(
        tmp_path,
        "pom.xml",
        """
        <?xml version="1.0"?>
        <!DOCTYPE project [<!ENTITY lol "lolol">]>
        <project><artifactId>x</artifactId></project>
        """,
    )
    with pytest.raises(ManifestParseError, match="DOCTYPE"):
        MavenAdapter().parse(path)


def test_maven_unresolved_property_left_verbatim(tmp_path: Path) -> None:
    """`_interpolate` documents that an unresolved `${...}` is left verbatim rather than guessed
    or silently dropped — inventing a value, or blanking the placeholder, would put a fact in the
    graph the pom's own bytes do not support (module docstring: "raw, as written")."""
    path = write(
        tmp_path,
        "pom.xml",
        """
        <?xml version="1.0"?>
        <project xmlns="http://maven.apache.org/POM/4.0.0">
          <artifactId>x</artifactId>
          <dependencies>
            <dependency>
              <groupId>g</groupId>
              <artifactId>a</artifactId>
              <version>${undefined.property}</version>
            </dependency>
          </dependencies>
        </project>
        """,
    )
    deps = MavenAdapter().parse(path)
    assert deps[0].version_spec == "${undefined.property}"


# --------------------------------------------------------------------------------------
# gradle
# --------------------------------------------------------------------------------------

BUILD_GRADLE_KTS = """
    plugins {
        kotlin("jvm") version "2.0.0"
        id("com.acme.conventions")
    }

    group = "com.acme"
    version = "1.4.0"

    repositories { mavenCentral() }

    dependencies {
        implementation("com.acme:acme-commons:1.4.0")
        implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.8.1")
        // implementation("com.acme:disabled:1.0")
        api(project(":shared"))
        implementation(libs.okhttp)
        compileOnly("org.projectlombok:lombok:1.18.32")
        testImplementation("org.junit.jupiter:junit-jupiter:5.10.2")
        runtimeOnly(group: 'com.h2database', name: 'h2', version: '2.2.224')
    }
"""


def test_gradle_script_parses_declaration_forms(tmp_path: Path) -> None:
    """A build script is a program, so the parse is best-effort by construction — but it must be
    honestly best-effort: commented-out lines, `project(':shared')` and unresolvable `libs.*`
    accessors are skipped rather than turned into fabricated coordinates."""
    adapter = GradleAdapter()
    path = write(tmp_path, "build.gradle.kts", BUILD_GRADLE_KTS)
    deps = adapter.parse(path)

    assert [(d.raw_id, d.scope, d.source_line) for d in deps] == [
        ("com.acme:acme-commons:1.4.0", "implementation", 12),
        ("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.8.1", "implementation", 13),
        ("org.projectlombok:lombok:1.18.32", "compileOnly", 17),
        ("org.junit.jupiter:junit-jupiter:5.10.2", "testImplementation", 18),
        ("com.h2database:h2:2.2.224", "runtimeOnly", 19),
    ]
    assert adapter.coordinate(deps[0]).key == "gradle:com.acme:acme-commons"


def test_gradle_version_catalog(tmp_path: Path) -> None:
    """`libs.versions.toml` IS a manifest (unlike the script), so `version.ref` indirection is
    resolved exactly — the catalog is where a modern build actually states its coordinates."""
    path = write(
        tmp_path,
        "gradle/libs.versions.toml",
        """
        [versions]
        okhttp = "4.12.0"
        junit = "5.10.2"

        [libraries]
        okhttp = { module = "com.squareup.okhttp3:okhttp", version.ref = "okhttp" }
        guava = { group = "com.google.guava", name = "guava", version = "33.2.1-jre" }
        junit = { module = "org.junit.jupiter:junit-jupiter", version.ref = "junit" }
        slf4j = "org.slf4j:slf4j-api:2.0.13"

        [plugins]
        kotlin = { id = "org.jetbrains.kotlin.jvm", version = "2.0.0" }
        """,
    )
    deps = GradleAdapter().parse(path)
    assert [(d.raw_id, d.version_spec) for d in deps] == [
        ("com.google.guava:guava:33.2.1-jre", "33.2.1-jre"),
        ("org.junit.jupiter:junit-jupiter:5.10.2", "5.10.2"),
        ("com.squareup.okhttp3:okhttp:4.12.0", "4.12.0"),
        ("org.slf4j:slf4j-api:2.0.13", "2.0.13"),
    ]


def test_gradle_catalog_version_require_strictly_prefer_forms(tmp_path: Path) -> None:
    """Gradle's rich version-catalog syntax (`{ require = "..." }` / `strictly` / `prefer`) is
    real, common syntax for range constraints — not just `version = "x"` or `version.ref`.
    Silently returning `None` for these would drop the `version_spec` from every catalog entry
    that uses a range constraint instead of a pin."""
    path = write(
        tmp_path,
        "gradle/libs.versions.toml",
        """
        [libraries]
        guava = { module = "com.google.guava:guava", version = { require = "[30.0,32.0)" } }
        """,
    )
    deps = GradleAdapter().parse(path)
    assert deps[0].version_spec == "[30.0,32.0)"


# --------------------------------------------------------------------------------------
# cargo
# --------------------------------------------------------------------------------------

CARGO_WORKSPACE = """
    [workspace]
    members = ["crates/core", "crates/cli"]
    resolver = "2"

    [workspace.dependencies]
    serde = { version = "1.0.203", features = ["derive"] }
    tokio = "1.38.0"

    [package]
    name = "acme-engine"
    version = "0.4.2"
    edition = "2021"

    [dependencies]
    anyhow = "1.0.86"
    rand_core = { version = "0.6.4", optional = true }
    yaml = { package = "serde_yaml", version = "0.9.34" }
    acme-shared = { path = "../shared" }

    [dev-dependencies]
    proptest = "1.5.0"

    [build-dependencies]
    cc = "1.0.99"

    [target.'cfg(unix)'.dependencies]
    nix = "0.29.0"
"""


def test_cargo_workspace_sections_and_renames(tmp_path: Path) -> None:
    """`yaml = { package = "serde_yaml" }` must resolve to the REAL crate: keying on the local
    alias would put one crates.io package on two nodes and lose the join between repos."""
    adapter = CargoAdapter()
    path = write(tmp_path, "Cargo.toml", CARGO_WORKSPACE)
    deps = adapter.parse(path)

    assert [(d.raw_id, d.scope) for d in deps] == [
        ("acme-shared", "normal"),
        ("anyhow", "normal"),
        ("rand_core", "normal"),
        ("serde_yaml", "normal"),
        ("proptest", "dev"),
        ("cc", "build"),
        ("nix", "target:normal"),
        ("serde", "workspace"),
        ("tokio", "workspace"),
    ]
    assert next(d for d in deps if d.raw_id == "rand_core").optional is True
    assert next(d for d in deps if d.raw_id == "serde_yaml").version_spec == "0.9.34"
    assert adapter.coordinate(deps[0]).key == "cargo::acme-shared"

    published = adapter.publishes(path)
    assert published is not None
    assert (published.key, published.version_spec) == ("cargo::acme-engine", "0.4.2")


def test_cargo_workspace_inheritance_states_no_version(tmp_path: Path) -> None:
    """`serde = { workspace = true }` genuinely does not state a version in this file. Inventing
    one would put a fact in the graph that the bytes do not support."""
    path = write(
        tmp_path,
        "Cargo.toml",
        """
        [package]
        name = "acme-cli"
        version.workspace = true

        [dependencies]
        serde = { workspace = true }
        """,
    )
    deps = CargoAdapter().parse(path)
    assert [(d.raw_id, d.version_spec) for d in deps] == [("serde", None)]
    published = CargoAdapter().publishes(path)
    assert published is not None and published.version_spec is None


def test_cargo_virtual_workspace_root_publishes_nothing(tmp_path: Path) -> None:
    """A virtual workspace root (`[workspace]` with no `[package]`) genuinely publishes no
    coordinate — `[package]` is what a Cargo crate uses to name itself. Fabricating one (e.g. a
    `None`/empty-string name) would put a fake crate node in the graph that nothing else in the
    workspace ever depends on."""
    path = write(
        tmp_path,
        "Cargo.toml",
        """
        [workspace]
        members = ["crates/core", "crates/cli"]
        resolver = "2"
        """,
    )
    assert CargoAdapter().publishes(path) is None


# --------------------------------------------------------------------------------------
# go
# --------------------------------------------------------------------------------------

GO_MOD = """
    module github.com/acme/billing/v2

    go 1.22

    require (
        github.com/google/uuid v1.6.0
        github.com/acme/shared v0.14.1
        google.golang.org/grpc v1.64.0
    )

    require (
        github.com/davecgh/go-spew v1.1.1 // indirect
        golang.org/x/net v0.26.0 // indirect
    )

    require github.com/spf13/cobra v1.8.0

    replace github.com/acme/shared => ../shared

    exclude (
        github.com/broken/pkg v0.0.1
    )
"""


def test_gomod_require_blocks_and_indirects(tmp_path: Path) -> None:
    """MVS puts the whole transitive closure in go.mod, so `// indirect` lines are real edges —
    dropping them under-orders any repo whose only path to a fleet sibling is indirect. `replace`
    and `exclude` blocks must not leak into the dependency list."""
    adapter = GomodAdapter()
    path = write(tmp_path, "go.mod", GO_MOD)
    deps = adapter.parse(path)

    assert [(d.raw_id, d.scope, d.source_line) for d in deps] == [
        ("github.com/google/uuid", "direct", 6),
        ("github.com/acme/shared", "direct", 7),
        ("google.golang.org/grpc", "direct", 8),
        ("github.com/davecgh/go-spew", "indirect", 12),
        ("golang.org/x/net", "indirect", 13),
        ("github.com/spf13/cobra", "direct", 16),
    ]
    assert deps[0].version_spec == "v1.6.0"

    coord = adapter.coordinate(deps[1])
    assert (coord.group, coord.name, coord.key) == (
        "github.com/acme",
        "shared",
        "go:github.com/acme:shared",
    )
    published = adapter.publishes(path)
    assert published is not None
    # The /v2 major suffix is part of the module identity: v1 and v2 are different modules.
    assert published.key == "go:github.com/acme:billing/v2"


def test_gomod_split_module_single_segment_and_missing_module_line(tmp_path: Path) -> None:
    """`_split_module` backs both `coordinate()` and `publishes()`. A single-segment module path
    (no `/` at all — a legitimate local/short Go module name) must land as `(group="", name=
    module)`, not be silently misparsed by falling through to the multi-segment branch; and a
    go.mod with no `module` line at all publishes nothing rather than a garbage coordinate."""
    coord = GomodAdapter().coordinate(RawDependency(raw_id="acme", version_spec="v1.0.0"))
    assert (coord.group, coord.name) == ("", "acme")

    path = write(tmp_path, "go.mod", "go 1.22\n")
    assert GomodAdapter().publishes(path) is None


# --------------------------------------------------------------------------------------
# registry: duplicates, tie-break, statelessness, fallback
# --------------------------------------------------------------------------------------


def test_duplicate_registration_raises_naming_the_collision(isolated_registry: None) -> None:
    """Two adapters answering to one name makes `manifests.adapter` provenance a lie, and the
    loser is chosen by import order. It is a startup error, never a silent overwrite."""

    class First(ManifestAdapter):
        name: ClassVar[str] = "twin"
        ecosystem: ClassVar[Ecosystem] = Ecosystem.NPM

        def matches(self, path: Path) -> bool:
            return False

        def parse(self, path: Path) -> list[RawDependency]:
            return []

        def coordinate(self, raw: RawDependency) -> object:  # pragma: no cover - never called
            raise AssertionError

    class Second(First):
        pass

    register(First)  # type: ignore[type-var]
    with pytest.raises(RuntimeError, match=r"duplicate manifest adapter name: twin.*Second"):
        register(Second)  # type: ignore[type-var]


def _make_adapter(adapter_name: str, adapter_priority: int) -> type[ManifestAdapter]:
    class _Fake(ManifestAdapter):
        name: ClassVar[str] = adapter_name
        ecosystem: ClassVar[Ecosystem] = Ecosystem.UNKNOWN
        priority: ClassVar[int] = adapter_priority

        def matches(self, path: Path) -> bool:
            return True

        def parse(self, path: Path) -> list[RawDependency]:
            return []

        def coordinate(self, raw: RawDependency) -> object:  # pragma: no cover - never called
            raise AssertionError

    return _Fake  # type: ignore[return-value]


def test_tiebreak_is_total_under_shuffled_import_order(isolated_registry: None) -> None:
    """THE run-equivalence guard (§12.21), exercised with a GENUINE shuffle rather than 3
    hand-written permutations standing in for one. `list.sort` is stable, so a bare `priority`
    key leaves equal-priority adapters in `pkgutil` import order — filesystem order,
    effectively — and the same repo then dispatches to different adapters across runs and
    yields different `Coordinate`s. Sorting by `(priority, name)` makes the order a function of
    the registry's CONTENTS alone, not of how they got there — which a 3-of-6-possible-orderings
    parametrize (the old form) cannot distinguish from a coincidence: 6 equal-priority names give
    720 orderings, and 20 real `random.shuffle` draws from a fixed seed (reproducible, not
    flaky) is what actually stresses that claim rather than re-describing it.
    """
    names = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]
    rng = random.Random(20260901)
    tables: set[tuple[str, ...]] = set()
    orders_seen: set[tuple[str, ...]] = set()
    for _ in range(20):
        order = names.copy()
        rng.shuffle(order)
        orders_seen.add(tuple(order))

        base.reset_adapters()
        for adapter_name in order:
            register(_make_adapter(adapter_name, 100))  # type: ignore[type-var]
        register(_make_adapter("last", 10_000))  # type: ignore[type-var]
        tables.add(tuple(a.name for a in adapters()))

    assert len(orders_seen) > 1, "the shuffle produced only one distinct order — not a real test"
    assert tables == {(*sorted(names), "last")}


def test_every_registered_adapter_is_stateless() -> None:
    """The registry holds ONE instance per class, shared across the runner's whole TaskGroup
    fan-out (§7.2). An adapter caching a parsed lockfile on `self` leaks repo A's dependencies
    into repo B — a wrong graph, not a crash. `discover()` enforces this mechanically; this test
    proves the enforcement is live."""
    for adapter in discover():
        assert vars(adapter) == {}, f"{adapter.name} carries instance state"


def test_discover_rejects_a_stateful_adapter(isolated_registry: None) -> None:
    """The `vars(inst) == {}` assertion must actually fire, or it is decoration."""
    base.reset_adapters()
    stateful = _make_adapter("hoarder", 100)()
    stateful._cache = {}  # type: ignore[attr-defined]
    base._ADAPTERS.append(stateful)
    with pytest.raises(RuntimeError, match=r"stateful ManifestAdapter 'hoarder'.*_cache"):
        discover(force=True)


def test_unrecognized_repo_falls_back_to_unknown(tmp_path: Path) -> None:
    """§3.1 step 2: the unknown-ecosystem path is a first-class path, not an error. A repo with
    no recognizable manifest must still appear in the fleet — dropping it silently is exactly
    what success criterion (c) forbids."""
    mystery = write(tmp_path, "Makefile.am", "SUBDIRS = src\n")
    adapter = adapter_for(mystery)

    assert adapter is not None
    assert adapter.name == "unknown"
    assert adapter.ecosystem is Ecosystem.UNKNOWN
    assert adapter.parse(mystery) == []
    assert UnknownAdapter().priority == 10_000

    # A real manifest is never stolen by the fallback: priority 10 000 can only ever win last.
    claimed = adapter_for(write(tmp_path, "package.json", '{"name": "x"}'))
    assert claimed is not None and claimed.name == "npm"


def test_unknown_adapter_coordinate_uses_unknown_namespace() -> None:
    """Whatever raw text the ADR-0008 class-2 LLM extraction slot produces for an unrecognized
    manifest is addressed in the UNKNOWN namespace, not silently attributed to some other
    ecosystem it happens to resemble — that would join an unresolved guess onto a real graph
    node."""
    coord = UnknownAdapter().coordinate(RawDependency(raw_id="mystery-thing", version_spec="1.0"))
    assert coord.ecosystem is Ecosystem.UNKNOWN
    assert (coord.group, coord.name, coord.version_spec) == ("", "mystery-thing", "1.0")


def test_matches_reads_no_files(tmp_path: Path) -> None:
    """`matches` is called for every path in a 250-repo walk. It is documented as a filename
    test; a file read there turns the scan into an I/O storm — and `matches` must answer for a
    path that does not exist on this host at all."""
    ghost = tmp_path / "nonexistent" / "pom.xml"
    assert MavenAdapter().matches(ghost) is True
    assert NpmAdapter().matches(ghost) is False


# --------------------------------------------------------------------------------------
# fail loud
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "body", "adapter"),
    [
        ("package.json", '{"dependencies": {"react": "^18",}}', NpmAdapter()),
        ("package.json", '{"dependencies": ["react"]}', NpmAdapter()),
        ("pyproject.toml", "[project\nname = 'x'\n", PythonAdapter()),
        ("requirements.txt", "git+https://example.com/x.git\n", PythonAdapter()),
        ("pom.xml", "<project><dependencies><dependency>", MavenAdapter()),
        ("pom.xml", "<project><dependencies><dependency><groupId>g</groupId>"
                    "</dependency></dependencies></project>", MavenAdapter()),
        ("libs.versions.toml", "[libraries\nx = 1\n", GradleAdapter()),
        ("libs.versions.toml", '[libraries]\nbad = { version = "1.0" }\n', GradleAdapter()),
        ("Cargo.toml", "[dependencies\nserde = '1'\n", CargoAdapter()),
        ("Cargo.toml", "dependencies = 3\n[dependencies]\n", CargoAdapter()),
        ("go.mod", "module x\nrequire (\n  github.com/a/b v1.0.0\n", GomodAdapter()),
        ("go.mod", "module x\nrequire github.com/a/b latest\n", GomodAdapter()),
    ],
)
def test_malformed_manifest_fails_loud_with_the_path(
    tmp_path: Path, filename: str, body: str, adapter: ManifestAdapter
) -> None:
    """Rule 11. A silent empty parse is indistinguishable from "declares no dependencies", so a
    broken manifest would drop every one of that repo's edges and the run would look healthy.
    The path must be in the message: at 250 repos, "invalid TOML" alone is unactionable."""
    path = write(tmp_path, filename, body)
    with pytest.raises(ManifestParseError) as excinfo:
        adapter.parse(path)
    assert str(path) in str(excinfo.value)


def test_unreadable_manifest_is_not_an_empty_manifest(tmp_path: Path) -> None:
    """A manifest that is not valid UTF-8, or is not there at all, raises rather than parsing to
    zero dependencies."""
    binary = tmp_path / "package.json"
    binary.write_bytes(b'{"name": "\xff\xfe bad"}')
    with pytest.raises(ManifestParseError, match="unreadable manifest"):
        NpmAdapter().parse(binary)

    with pytest.raises(ManifestParseError, match="unreadable manifest"):
        GomodAdapter().parse(tmp_path / "absent" / "go.mod")


# --------------------------------------------------------------------------------------
# determinism across processes
# --------------------------------------------------------------------------------------

_PROBE = """
import json, sys
from pathlib import Path
from fleet.manifests.base import discover, adapter_for

out = []
for name in sorted(p.name for p in Path(sys.argv[1]).iterdir()):
    path = Path(sys.argv[1]) / name
    adapter = adapter_for(path)
    assert adapter is not None
    out.append([adapter.name, [
        [d.raw_id, d.version_spec, d.scope, adapter.coordinate(d).key]
        for d in adapter.parse(path)
    ]])
out.append([[a.name, a.priority] for a in discover()])
sys.stdout.write(json.dumps(out))
"""


def _probe(fixtures: Path, hash_seed: str) -> bytes:
    env = dict(os.environ, PYTHONHASHSEED=hash_seed, PYTHONPATH=str(SRC))
    result = subprocess.run(  # noqa: S603 - fixed argv, this interpreter, no shell
        [sys.executable, "-c", _PROBE, str(fixtures)],
        capture_output=True,
        check=True,
        env=env,
    )
    return result.stdout


def test_parsing_is_byte_identical_across_processes(tmp_path: Path) -> None:
    """Same bytes in, same values out, in the same ORDER — under a different `PYTHONHASHSEED`,
    in a fresh interpreter, with `pkgutil` free to import adapters in any order. Dict/set
    iteration order and registry import order are the two ways this silently drifts, and a
    drifting adapter order changes which adapter claims a path, which changes the §12.21
    run-equivalence digest of an otherwise identical run."""
    fixtures = tmp_path / "fixtures"
    write(fixtures, "package.json", PACKAGE_JSON)
    write(fixtures, "pyproject.toml", PYPROJECT)
    write(fixtures, "pom.xml", POM)
    write(fixtures, "Cargo.toml", CARGO_WORKSPACE)
    write(fixtures, "go.mod", GO_MOD)
    write(fixtures, "build.gradle.kts", BUILD_GRADLE_KTS)

    first = _probe(fixtures, "0")
    second = _probe(fixtures, "1")
    third = _probe(fixtures, "12345")

    assert first == second == third
    assert json.loads(first)[-1][-1] == ["unknown", 10_000]
