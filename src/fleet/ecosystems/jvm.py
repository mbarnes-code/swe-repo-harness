"""JVM: `MAVEN` + `GRADLE` → `java/`, via `rules_jvm_external` / `maven.install` (§3.3, §7.5).

**One adapter, two ecosystems.** Maven and Gradle are two manifest *formats* — §7.3's business —
that produce the same artifacts, the same `groupId:artifactId` address space and the same Bazel
rules. Splitting them would make `ecosystems/gradle.py` a byte-copy of this file, and a byte-copy
is where the two halves silently drift apart. This is exactly the case §7.5's `ecosystems`
frozenset exists for.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar, Final

from fleet.ecosystems.base import (
    EcosystemAdapter,
    join_segments,
    register,
    select_entrypoint,
    target_name,
)
from fleet.models.build import BuildTarget, BuildUnit, ToolchainRequirement, WorkspaceDep
from fleet.models.enums import ContractKind, Ecosystem
from fleet.models.repo import Coordinate

_SOURCE_ROOTS: Final[tuple[str, ...]] = ("src/main/java/", "src/main/kotlin/", "src/main/scala/")
"""Maven's Standard Directory Layout, which Gradle's `java` plugin adopts verbatim. The package
of a class is its path *below* one of these roots — the one fact a `main_class` needs."""

_TEST_SOURCE_ROOTS: Final[tuple[str, ...]] = (
    "src/test/java/",
    "src/test/kotlin/",
    "src/test/scala/",
)
"""The Standard Directory Layout's test half — the one fact a `test_class` needs (D144)."""


@register
class JvmAdapter(EcosystemAdapter):
    """`java/<group_path>/<artifact>`; `maven.install` for external deps."""

    name: ClassVar[str] = "jvm"
    ecosystems: ClassVar[frozenset[Ecosystem]] = frozenset({Ecosystem.MAVEN, Ecosystem.GRADLE})
    monorepo_dir: ClassVar[str] = "java"
    ruleset: ClassVar[str | None] = "rules_jvm_external"
    extension: ClassVar[str | None] = "maven.install"
    extension_bzl: ClassVar[Mapping[str, str]] = {
        # The one ruleset of the five whose extension really is at `//:extensions.bzl` — read off
        # rules_jvm_external 6.7, where it re-exports `private/extensions/maven.bzl`.
        "maven": "@rules_jvm_external//:extensions.bzl",
    }
    repo_name: ClassVar[str | None] = "maven"
    library_rule: ClassVar[str] = "java_library"
    binary_rule: ClassVar[str | None] = "java_binary"
    test_rule: ClassVar[str | None] = "java_test"
    library_bzl: ClassVar[str | None] = "@rules_java//java:defs.bzl"
    binary_bzl: ClassVar[str | None] = "@rules_java//java:defs.bzl"
    test_bzl: ClassVar[str | None] = "@rules_java//java:defs.bzl"
    entrypoints: ClassVar[tuple[str, ...]] = ("Main.java", "Application.java", "App.java")
    src_suffixes: ClassVar[tuple[str, ...]] = (".java", ".srcjar")
    """`javac` refuses anything else. A `.properties` bundle in `srcs` fails the compile; the
    same file in `resources` is packaged, which is where `non_source_files()` sends it."""
    contract_bindings: ClassVar[Mapping[ContractKind, str]] = {
        ContractKind.PROTO: "java_proto_library",
        ContractKind.AVRO: "java_avro_library",
        ContractKind.THRIFT: "java_thrift_library",
        # OPENAPI has no language-native rule: §7.6's OPENAPI ContractAdapter wraps the
        # configured generator in a `genrule`, and the per-language part is the generator's
        # own flag, not a different rule. SHARED_LIB is delegated wholesale back to
        # `generate_targets` (§3.3), so it names this adapter's own library rule.
        ContractKind.OPENAPI: "genrule",
        ContractKind.SHARED_LIB: "java_library",
    }

    def path_tail(self, coordinate: Coordinate) -> str:
        """`<group_path>/<artifact>` — `com.acme:commons` → `com/acme/commons`.

        The group is a directory tree rather than one segment because a `groupId` is a reversed
        DNS name and 250 repos of `com.acme.*` flattened into one level is a directory listing
        no reviewer can read.
        """
        return join_segments(coordinate.group_path, coordinate.name)

    def import_specifier(self, coordinate: Coordinate, dest: str) -> str:
        """`<group>.<artifact>` — the Java package an `import` names, unchanged by relocation.

        A `.java` file's `package`/`import` statements are decided by the declaration in the file
        and by `groupId`/`artifactId` convention, not by the directory a build system reads it
        from, so migrating `com.acme:commons` into `java/com/acme/commons` does not move a single
        symbol. `java_library` carries the classpath; the source text does not change. Returning
        the *label* here would be a `package //java/com/acme/commons:commons;` that `javac`
        rejects at the first line of every file.
        """
        _ = dest
        group = coordinate.group.strip(".")
        return f"{group}.{coordinate.name}" if group else coordinate.name

    def workspace_deps(self, unit: BuildUnit) -> list[WorkspaceDep]:
        """One `maven.install` artifact per coordinate, with **no `attrs`** — deliberately.

        `maven.install` is the one dialect of the five that pins a version per artifact, so the
        version must be §3.3 step 3's MVS winner and not the spec as this repo happened to
        declare it. Leaving `attrs` empty is what arms `render_module_bazel`'s guard: a
        `WorkspaceDep` with neither `attrs` nor `resolved_version` is refused outright, so a
        maven artifact can never reach `MODULE.bazel` carrying an unreconciled version.
        """
        return [
            WorkspaceDep(
                ruleset="rules_jvm_external",
                extension="maven.install",
                coordinate=coordinate,
                repo_name="maven",
            )
            for coordinate in sorted(unit.external_coordinates, key=lambda c: c.key)
            if coordinate.ecosystem in self.ecosystems
        ]

    def generate_targets(self, unit: BuildUnit) -> list[BuildTarget]:
        """One `java_library` over the module's sources, plus a `java_binary` iff it has a main.

        `resources` become the library's `resources` attribute rather than `srcs`: `javac` would
        refuse a `.properties` file in `srcs`, and dropping them instead would produce a library
        that compiles and then fails at runtime looking for a bundle that was never packaged.
        """
        name = target_name(unit)
        deps = self.dep_labels(unit)
        srcs = self.sources(unit)
        resources = self.non_source_files(unit)
        library = BuildTarget(
            package=unit.dest,
            name=name,
            rule="java_library",
            load_from="@rules_java//java:defs.bzl",
            srcs=srcs,
            deps=deps,
            attrs={"resources": resources} if resources else {},
            visibility=["//visibility:public"],
        )
        targets = [library]
        entry = select_entrypoint(srcs, self.entrypoints)
        if entry is not None:
            targets.append(
                BuildTarget(
                    package=unit.dest,
                    name=f"{name}_bin",
                    rule="java_binary",
                    load_from="@rules_java//java:defs.bzl",
                    deps=[f":{name}"],
                    attrs={"main_class": _main_class(entry)},
                    visibility=["//visibility:private"],
                )
            )
        return targets

    def test_targets(self, unit: BuildUnit) -> list[BuildTarget]:
        """One `java_test` **per discovered test source file**, each depending on the library
        rather than re-compiling it — a second compilation of the same sources is where "the test
        passed against a different classpath than the build" comes from.

        `test_class` is set explicitly (D144) per target, derived from that file's own path below
        `_TEST_SOURCE_ROOTS` — the same source-root convention `generate_targets` already uses for
        `main_class`. A single target's name (`{name}_test`, snake_case) never satisfies Bazel's
        CamelCase-target-name inference convention, so an unset `test_class` is not a style gap:
        Bazel guesses `<package>.<target-name>` from the target's own label and fails at **test
        time**, not at build time, with "Class not found" — the target analyzes and builds clean.
        One target per file is what closes this correctly for a `dest` with more than one test
        class: bundling every discovered test source into a *single* target with one `test_class`
        would compile every file but execute only one's `@Test` methods while still reporting a
        passing build — silently dropping every other file's tests, which is worse than the
        original "Class not found" failure it would otherwise replace. `srcs` still names the
        **full** `test_srcs` set on every target (not just that target's own file) so files that
        reference shared test helpers still compile; only `test_class` (and the target's name)
        differ per file. The first (sorted) file keeps the un-suffixed `{name}_test` name so the
        common one-test-file case (this adapter's own e2e fixture) is unaffected; subsequent files
        get `{name}_test_N` in the same sorted order, which is deterministic and stable across runs
        because `test_sources(unit)` already returns a sorted list.
        """
        test_srcs = self.test_sources(unit)
        if not test_srcs:
            return []
        name = target_name(unit)
        return [
            BuildTarget(
                package=unit.dest,
                name=f"{name}_test" if i == 0 else f"{name}_test_{i}",
                rule="java_test",
                load_from="@rules_java//java:defs.bzl",
                srcs=test_srcs,
                deps=[f":{name}", *self.external_labels(unit)],
                attrs={"test_class": _test_class(entry)},
                testonly=True,
                visibility=["//visibility:private"],
            )
            for i, entry in enumerate(test_srcs)
        ]

    def toolchain_requirements(self) -> list[ToolchainRequirement]:
        """Deliberately empty (Rule 11 over Rule 2: an honest gap, not an invented pin).

        A JDK toolchain is registered by `rules_java`, which is a transitive `bazel_dep` of
        `rules_jvm_external` and is **not** a key in `build.ruleset_versions` (§9). Emitting a
        requirement against an unpinned ruleset would make `render_module_bazel` raise on every
        JVM repo, and inventing a pin here would put a version in Python that §9 says only the
        operator sets. Pin `rules_java` in `build.ruleset_versions` and this method is where the
        `java_toolchains.toolchain` tag goes.
        """
        return []


def _main_class(entry: str) -> str:
    """`src/main/java/com/acme/App.java` → `com.acme.App`.

    The package is the path below the source root, which is the JVM's actual rule; guessing it
    from the file name alone would emit a `main_class` that `java` cannot load, and a
    `java_binary` that builds green and dies on `bazel run`.
    """
    return _fully_qualified_class(entry, _SOURCE_ROOTS)


def _test_class(entry: str) -> str:
    """`src/test/java/com/acme/WidgetTest.java` → `com.acme.WidgetTest` (D144).

    Same derivation as `_main_class`, against the test half of the Standard Directory Layout —
    `test_targets()` needs this because its own target name (`{name}_test`) never satisfies
    Bazel's CamelCase-target-name `test_class` inference convention.
    """
    return _fully_qualified_class(entry, _TEST_SOURCE_ROOTS)


def _fully_qualified_class(path: str, roots: tuple[str, ...]) -> str:
    """`<root>/<package>/<Class>.java` → `<package>.<Class>`, for whichever `roots` names the
    Standard Directory Layout half (`main` or `test`) the caller is deriving a class name from."""
    for root in roots:
        index = path.find(root)
        if index != -1:
            path = path[index + len(root) :]
            break
    else:
        path = path.rsplit("/", maxsplit=1)[-1]
    stem = path.rsplit(".", maxsplit=1)[0]
    return stem.replace("/", ".")
