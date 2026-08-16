"""Go: `GO` → `go/`, via `rules_go` + Gazelle / `go_deps.from_file` (§3.3, §7.5).

**The one delegating adapter.** `uses_gazelle = True` means `generate_targets` returns `[]` and
the §3.3 driver runs Gazelle instead, then reads back what it produced with `bazel query`. That
is not a gap: `rules_go` ships a generator that resolves every `import` in the tree to a label,
which is strictly better than anything this file could infer from a `BuildUnit`'s `srcs` list,
and an ABC that assumed every ecosystem emits targets the same way would have forced this module
to lie about what it does. Emitting a half-right `go_library` here and letting Gazelle overwrite
it would also make the generated tree depend on which of the two ran last.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import ClassVar

from fleet.ecosystems.base import (
    AdapterCoordinateError,
    EcosystemAdapter,
    join_segments,
    register,
)
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

_GO_VERSION = "1.24.12"
"""The ONE Go version, and it is a floor imposed by the PINNED rulesets, not a preference.

It reaches five places at once — `go_sdk.download(version = …)` in the generated `MODULE.bazel`
(`toolchain_requirements`), the resolver's `GOTOOLCHAIN` (`resolution`), the `go` directive of
the union `go.mod` (`_go_mod_text`), the `GOTOOLCHAIN` the vendored `tools/bin/go` wrapper
enforces, and — transitively — the root `go.sum`, whose hashes were taken against that `go.mod`
by that toolchain. A version raised in one of them and not the others is what the wrapper's
`GOTOOLCHAIN` pin exists to turn into a loud failure rather than silently different sums.

**Why 1.24.12 and not 1.23.4 (measured under real Bazel 9.2.0).** `build.ruleset_versions` pins
`gazelle` at 0.52.2 and `rules_go` at 0.61.1, and both sit above a 1.23.4 SDK:

* gazelle 0.52.2's `go.work` requires `go >= 1.24.12`, so the `go_repository` tools `go_deps`
  builds to generate each fetched module's BUILD files refuse to compile — `failed to build
  tools: go: go.work requires go >= 1.24.12 (running go 1.23.4; GOTOOLCHAIN=local)`, for ANY Go
  repo with ANY external dependency;
* rules_go 0.61.1's own `go.mod` declares `go 1.24.0`, so the floor is not gazelle's alone.

Lowering `gazelle` instead does not exist as an option: rules_go 0.61.1's `MODULE.bazel` itself
declares `bazel_dep(name = "gazelle", version = "0.51.3")`, and v0.51.3's `go.mod` declares the
same `go 1.24.12`. The newest gazelle below that floor is v0.47.0 (`go 1.22.9`; v0.48.0 is the
release that moved to 1.24.12), which predates the Bazel 9 fixes the table's own note records —
0.39.1 fails there through rules_go, and pinning a 0.4x gazelle under a rules_go that floors
0.51.3 is a downgrade past a dependency's own requirement, not a supported pair. So the gazelle
that a 1.23.4 SDK could build is not one this Bazel and this rules_go can load. Raising the SDK
is the only move that keeps both load-probed pins.

1.24.12 exactly, rather than anything newer: it is the minimum both floors accept, so the
smallest step that makes the pins jointly possible."""

_GO_MOD_FILE = "go.mod"
_GO_MOD_LABEL = f"//:{_GO_MOD_FILE}"
"""The label and the file from ONE constant (D10): `go_deps.from_file` reads the root `go.mod`,
and a label whose file no phase creates is `Error in read` from inside Gazelle's extension."""

_GO_SUM_FILE = "go.sum"
"""The root `go.sum` — named by no label, read by `sums_from_go_mod` because it sits BESIDE the
`go.mod` `go_deps.from_file` names. A file with no label is exactly why it went missing (ADR-0050):
nothing in the generated text mentions it, so no D10 check over `//:` references could see it."""

_MONOREPO_MODULE = "fleet.internal/monorepo"
"""The `module` line of the root `go.mod` — the MONOREPO's own path, never any one repo's.

`deps_from_go_mod` consumes this value **only** as the main module path; the `require` list is
what becomes the `@com_github_…` repos. So the module line has exactly one job — to name a module
that is not any of the fleet's — and naming one repo's published path instead made the root file
a claim about that repo, which is half of the collision ADR-0050 recorded. It costs nothing in
import paths: `# gazelle:prefix <published path>` is written per package (`gazelle_config`), so
each relocated repo still compiles against the path it was published under.

Not resolvable on any proxy, deliberately: a main module is never fetched (measured — `go list -m
all` prints it and fetches nothing), and a path that *could* resolve would invite exactly that."""

_MODULE_PATH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]*(?:/[A-Za-z0-9][A-Za-z0-9._~-]*)*")
"""`module.CheckPath`'s shape, as far as a `require` line needs it: non-empty `/`-joined elements,
each starting alphanumeric, and no `.`/`..` element (neither can match). The dot-in-first-element
rule is checked separately in `_require_line`, because it is the one `go` names in its own error."""

_MODULE_VERSION_RE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+incompatible)?")
"""A canonical Go module version. Build metadata other than `+incompatible` is not a legal module
version, and a bare `v0.0.0` — `_go_mod_text`'s old default for a coordinate whose manifest named
no version — matches here but is rejected by `_require_line`, which is where that rule belongs."""

_RESOLVER: tuple[str, ...] = ("go", "mod", "download", "all")
"""The one command that produces a usable `go.sum`, measured against real Go (ADR-0050).

Neither of the two obvious alternatives works, and both fail in ways the driver's guards do not
fully catch:

* `go mod tidy` — the resolver's scratch directory holds `plan.inputs` and NOTHING else, so it
  contains no `.go` source. With nothing imported, every requirement is unused: `tidy` prints
  `warning: "all" matched no packages`, **deletes the entire `require` block from its own input**
  and writes no `go.sum` at all. The "exited 0 but wrote no usable lock" guard fires — after the
  command has destroyed the file it was resolving.
* `go mod download` (bare) — writes only the `/go.mod` hash lines and **no `h1:` module-zip
  hash**. The resulting file is non-empty, so it slips past that same guard, and
  `sums_from_go_mod` — which reads for the `h1:` line — still cannot use it. A silent half-answer
  is worse than a loud absence.

`go mod download all` writes both hash kinds for the full transitive closure (a `go.mod` naming
only `testify` yields sums for five modules) and leaves its input byte-identical: `-mod=readonly`
is the default since Go 1.16, so the resolver cannot rewrite the `go.mod` it was handed, and an
unresolvable pin errors instead of being quietly relaxed."""


class GoModuleCoordinateError(AdapterCoordinateError):
    """A coordinate that claims to be a Go module cannot be written as a `require` line (Rule 11).

    Raised rather than skipped, and rather than written out anyway, because both alternatives are
    silent in the way this whole file has been: skipping drops a dependency from the module graph
    with nothing raised anywhere, and writing it produces a root `go.mod` that `go` refuses — at
    PARSE time, which means the failure names a file this harness invented and no repo, while
    taking every other Go repo's requirements down with it. Naming the coordinate here says which
    repo's manifest to look at.

    It cannot fire on anything `manifests/gomod.py` produced: that parser rejects a requirement
    whose version does not start with `v` (`ManifestParseError`) and takes the module path
    verbatim from a `go.mod` `go` already accepted. So this is a guard on coordinates that reached
    a Go unit by some other route, which is exactly the case that used to render `left-pad ^1.3.0`
    into the fleet's root module file.

    **A subclass of `base.AdapterCoordinateError`, and that lineage is what makes the failure
    CONTAINED rather than fatal.** Loud and global are different things: as a bare `ValueError`
    this escaped §3.3 step 2's per-ecosystem containment — which catches only what the driver
    knows about — and one malformed Go coordinate ended the whole fleet's build instead of that
    ecosystem's repos. The driver cannot catch this class by name without knowing an ecosystem
    exists (§12.6), so the neutral base is the type it catches and this one carries the Go
    message. `ValueError` remains in the lineage through that base, so anything already catching
    it is unaffected.
    """


@register
class GoAdapter(EcosystemAdapter):
    """`go/<module-tail>`; Gazelle generates the BUILD files, `go_deps.from_file` the deps."""

    name: ClassVar[str] = "go"
    ecosystems: ClassVar[frozenset[Ecosystem]] = frozenset({Ecosystem.GO})
    monorepo_dir: ClassVar[str] = "go"
    ruleset: ClassVar[str | None] = "rules_go"
    extension: ClassVar[str | None] = "go_deps.from_file"
    extension_bzl: ClassVar[Mapping[str, str]] = {
        # `go_deps` is NOT in rules_go: Gazelle owns it (`@gazelle//:extensions.bzl`), which is
        # why `workspace_deps` below declares `gazelle` as the `bazel_dep` that carries it. The
        # SDK extension is rules_go's own, at `go/extensions.bzl`. Both read off the rulesets.
        "go_deps": "@gazelle//:extensions.bzl",
        "go_sdk": "@io_bazel_rules_go//go:extensions.bzl",
    }
    ruleset_repo_names: ClassVar[Mapping[str, str]] = {"rules_go": "io_bazel_rules_go"}
    """`rules_go` is visible as `@io_bazel_rules_go`, and this is the ONE spelling of it.

    Gazelle — the generator this adapter delegates its BUILD files to — writes
    `load("@io_bazel_rules_go//go:def.bzl", …)` into every package it creates, so a monorepo whose
    `bazel_dep(name = "rules_go")` carries no `repo_name` cannot LOAD a single Go package: `No
    repository visible as '@io_bazel_rules_go' from main repository`, before analysis, for every
    Go repo in the fleet. Upstream `bazel-gazelle` declares exactly this
    (`bazel_dep(name = "rules_go", …, repo_name = "io_bazel_rules_go")`), which is why the fix is
    to agree with the generator rather than to re-point it with `# gazelle:map_kind`.

    A `bazel_dep` has exactly one apparent name, so `extension_bzl["go_sdk"]` above spells the
    same module the same way. It used to say `@rules_go`, which was invisible the moment the
    `repo_name` landed — `no repo visible as '@rules_go' here`, raised while Bazel computes the
    main repository mapping, i.e. earlier than the error it replaced."""
    repo_name: ClassVar[str | None] = None
    """`go_deps` names one `@`-repo **per module** (`com_github_acme_commons`), not one hub, so
    the repo name is derived per coordinate rather than fixed on the class."""

    library_rule: ClassVar[str] = "go_library"
    binary_rule: ClassVar[str | None] = "go_binary"
    test_rule: ClassVar[str | None] = "go_test"
    library_bzl: ClassVar[str | None] = "@io_bazel_rules_go//go:def.bzl"
    binary_bzl: ClassVar[str | None] = "@io_bazel_rules_go//go:def.bzl"
    test_bzl: ClassVar[str | None] = "@io_bazel_rules_go//go:def.bzl"
    uses_gazelle: ClassVar[bool] = True
    contract_bindings: ClassVar[Mapping[ContractKind, str]] = {
        ContractKind.PROTO: "go_proto_library",
        ContractKind.OPENAPI: "genrule",
        ContractKind.SHARED_LIB: "go_library",
    }

    def path_tail(self, coordinate: Coordinate) -> str:
        """`<module-tail>` — the module path **below its host and org**.

        `github.com/acme/commons` → `commons`, not `github.com/acme/commons`: the host and org
        are the same for every repo in one fleet, so keeping them would add two directory levels
        that carry no information, and `# gazelle:prefix` (below) is what preserves the *full*
        import path that Go code actually compiles against.
        """
        return join_segments(coordinate.name)

    def import_specifier(self, coordinate: Coordinate, dest: str) -> str:
        """The Go module path — `github.com/acme/commons` — which is what `import` spells.

        Preserved rather than re-rooted at `dest`, and `gazelle_config`'s
        `# gazelle:prefix <module path>` is what makes that true: Gazelle resolves every import
        against the prefix, so the package that now lives in `go/commons/` still compiles against
        its published path and no `.go` file's import block is touched. That is the same fact
        `path_tail` reads from the other side — one coordinate, two uses.
        """
        _ = dest
        group = coordinate.group.strip("/")
        return f"{group}/{coordinate.name}" if group else coordinate.name

    def workspace_deps(self, unit: BuildUnit) -> list[WorkspaceDep]:
        """`go_deps.from_file(go_mod = …)` — the module graph comes from `go.mod`, whole.

        Go's own MVS already ran when `go.mod` was written, so restating each requirement as a
        separate tag would let this harness's reconciliation and the Go toolchain's disagree,
        with `go build` and `bazel build` then resolving different versions of the same module.
        """
        return [
            WorkspaceDep(
                # `gazelle`, not `rules_go`: `go_deps` is defined in Gazelle's module, so the
                # `bazel_dep` the emitted `use_extension("@gazelle//…")` needs is Gazelle's. A
                # `bazel_dep(rules_go)` alone leaves `@gazelle` invisible to the root module and
                # MODULE.bazel fails to load for every Go repo in the fleet.
                ruleset="gazelle",
                extension="go_deps.from_file",
                coordinate=coordinate,
                repo_name=_bazel_repo_name(coordinate),
                attrs={"go_mod": _GO_MOD_LABEL},
            )
            for coordinate in sorted(unit.external_coordinates, key=lambda c: c.key)
        ]

    def workspace_files(self, units: Sequence[BuildUnit]) -> list[SupportFile]:
        """`//:go.mod` — the MONOREPO's own module over the UNION of every Go unit's requirements
        — and `//:go.sum` beside it, which is neither carried nor synthesized (see below).

        **Fleet-wide over every Go unit, not per repo**: `go_deps.from_file` names ONE `//:go.mod`
        at the monorepo root, so a per-unit view offers N candidates for that single path and
        `union_workspace_files`' first-writer-wins `setdefault` kept whichever it saw first. That
        is ADR-0050 step 2's defect and this is the fix: with two Go repos the root used to
        declare one repo's module path and one repo's `require` block, and the other repo's
        dependencies were absent from the module graph entirely — with nothing raised, because
        `cli._fleet_support_files` hands every plan of an ecosystem the identical tuple, so
        `RootFileConflictError` never sees two candidates and the drop happens upstream of it.

        **`carry_from` is DROPPED**, for `js.py`'s `pnpm-workspace.yaml` reason in Go's dialect:
        one repo's `go.mod` declares THAT repo's module and THAT repo's requirements, and the file
        the monorepo root needs is a statement about the fleet, which no single repo's file is.
        Promoting one would also — because `cli._carried` short-circuits the resolver for any file
        with a real candidate behind it — do it *without any resolver running*. The repo's own
        `go.mod` is not lost; Phase 2 left it at `<dest>/go.mod`.

        **This does NOT contradict `workspace_deps`' "`go.mod` is the resolution".** The versions
        written here are the ones Phase 1 read out of the repos' own `go.mod` files, verbatim; the
        harness re-states nothing and reconciles nothing. Where two repos pin the same module at
        different versions BOTH lines are emitted and Go's own MVS takes the higher — measured
        against real `go`, which resolved a `go.mod` requiring `testify` at both `v1.9.0` and
        `v1.8.0` to `v1.9.0` and reported no error. That is the same "let the Go toolchain's MVS
        decide, never this harness's" rule, applied to the union instead of to a single file.
        """
        if not any(unit.external_coordinates for unit in units):
            return []
        requires = _go_requires(units)
        files = [
            SupportFile(path=_GO_MOD_FILE, carry_from=[], content=_go_mod_text(requires))
        ]
        if requires:
            # NEITHER carried NOR synthesized — `resolution()` below is the only thing permitted
            # to fill it. Both of the other two sources are actively wrong here:
            #
            # * **carried** — a `go.sum` is a list of content hashes valid ONLY against the
            #   `go.mod` sitting beside it. One repo's sums next to the fleet's unioned `go.mod`
            #   are missing hashes for what it requires and stale for what it does not, which
            #   manufactures a checksum mismatch — the one class of failure a migration harness
            #   must never invent. `carry_from=[]` is also what keeps `cli._carried` from
            #   short-circuiting the resolve entirely (the JS workspace-lock rule of ADR-0048,
            #   not the one-contributor rule `py.py` uses for `requirements.lock`).
            # * **synthesized** — an `h1:` line is a hash of a module zip, so a floor written here
            #   would be either absent or WRONG, and a wrong sum fails as a security check.
            #
            # Declared only alongside a non-empty `require` block, and gated on the SAME predicate
            # `resolution()` is: `sums_from_go_mod` is called only when the `go.mod` carries a
            # requirement, so with none there is nothing to verify and nothing to resolve — and an
            # empty `go.sum` with no resolver behind it is precisely the "well-formed file that
            # produces an empty dependency hub" this path exists to stop writing.
            files.append(SupportFile(path=_GO_SUM_FILE, carry_from=[], content=""))
        return files

    def resolution(self, units: Sequence[BuildUnit]) -> Resolution | None:
        """`go mod download all` over the root `go.mod` → the `go.sum` beside it (ADR-0050).

        **This is the file `go_deps.from_file` was never given.** `go_deps.bzl` calls
        `sums_from_go_mod` whenever the `go.mod` it loads carries any `require`, and that function
        reads a `go.sum` next to it. The root `go.mod` has carried `require` lines since the
        adapter was written and no phase has ever produced the sums, so the extension had nothing
        to verify the module zips against.

        **The input is the UNION — the exact bytes `workspace_files` puts at the root — and that
        replaces the carry-only input this seam shipped with.** The two were in direct conflict:
        a union is synthesized by construction, so no repo's carried file is it, and sums resolved
        from one repo's `go.mod` beside a unioned one at the root are missing hashes for every
        other repo's modules. Every hash in such a file would be individually correct and the file
        as a whole would be wrong — which is the checksum mismatch `workspace_files` above refuses
        to manufacture, arriving by a back door. One renderer, called twice, is the only shape in
        which "the sums hash the `go.mod` that lands" is true by construction rather than by a
        selection rule that has to be kept in step by hand.

        **What that costs, and why the cost is paid rather than avoided.** ADR-0050 fact 3 chose
        carry-only because the *old* synthesized floor was unusable: a coordinate whose manifest
        declared no version rendered `v0.0.0`, which `go` rejects (`invalid version: unknown
        revision v0.0.0`), and a coordinate from a non-Go manifest rendered `left-pad ^1.3.0`,
        which `go` refuses at PARSE time (`malformed module path "left-pad": missing dot in first
        path element`). That was a property of the renderer, not of synthesis: `_require_line` now
        emits only what `go` accepts and raises `GoModuleCoordinateError` naming the coordinate
        otherwise (Rule 11), so the failure arrives here — at the harness, naming the offending
        dependency — instead of inside `go` naming a file this harness invented. What is genuinely
        given up is the driver's "neither a carried file nor a synthesized floor produced any
        content" guard, which carry-only used to reach: a synthesized input always has content, so
        a Go repo that shipped no `go.mod` at all no longer trips it. It trips nothing instead —
        the union simply contains no line for a repo that declared no requirement — which is the
        honest answer, because Phase 1 read those coordinates out of manifests, not out of thin
        air, and a repo with no Go dependencies owes the module graph nothing.

        **One resolve over every Go unit.** `go_deps.from_file(go_mod = "//:go.mod")` names ONE
        file at the root, so a per-repo resolve would produce N candidate `go.sum`s for one path
        and the driver would keep the first. Gated on the union being non-empty rather than on any
        unit having coordinates: `go mod download all` over a `go.mod` with no `require` writes no
        `go.sum`, which the driver's "exited 0 but wrote no usable lock" guard would report as a
        resolver failure for a fleet whose Go repos genuinely have no Go dependencies.
        """
        requires = _go_requires(units)
        if not requires:
            return None
        return Resolution(
            lock_path=_GO_SUM_FILE,
            argv=list(_RESOLVER),
            # The SAME text `workspace_files` declares for `//:go.mod`, from the same two pure
            # functions — not a second rendering that happens to agree today.
            inputs=[
                SupportFile(path=_GO_MOD_FILE, carry_from=[], content=_go_mod_text(requires))
            ],
            # **The sums must be the ones the SDK Bazel fetches would compute, so the resolver's
            # toolchain is pinned to the SAME `_GO_VERSION` the `go_sdk` toolchain below
            # registers.** Without this the selection is `GOTOOLCHAIN`'s default `auto`, which is
            # a property of the operator's host and not of this fleet: `auto` silently downloads
            # and switches to whatever newer toolchain a `go.mod` asks for, `local` refuses the
            # same `go.mod` outright, and a host that exports either one makes the harness write
            # different `go.sum` bytes for one unchanged input. It is read off the constant rather
            # than spelled again, because a second copy of the version is how the resolver's
            # toolchain and the registered SDK drift apart — and a sums file computed by a
            # different SDK than the one that verifies it surfaces inside Bazel as a checksum
            # mismatch, which reads as a supply-chain compromise.
            env={"GOTOOLCHAIN": f"go{_GO_VERSION}"},
        )

    def generate_targets(self, unit: BuildUnit) -> list[BuildTarget]:
        """`[]`, always — §12.32's invariant for a `uses_gazelle` adapter. Gazelle emits the
        targets and `bazel/emit.py` reads them back with `bazel query`."""
        return []

    def test_targets(self, unit: BuildUnit) -> list[BuildTarget]:
        """`[]` for the same reason: `go_test` is Gazelle's output, from `*_test.go`."""
        return []

    def gazelle_config(self, unit: BuildUnit) -> GazelleConfig:
        """The directives written *before* the generator runs.

        `# gazelle:prefix` is the load-bearing one: it is the module path Gazelle resolves every
        intra-repo `import` against, so an absent or wrong prefix turns every internal import
        into an unresolvable external dependency and the whole package fails to build for a
        reason that never mentions the prefix. It comes from the unit's published coordinate,
        which is the same fact `path_tail` reads — one source, two uses.

        **`args` are the flags the DRIVER must pass to the one fleet-wide invocation, and both
        were chosen by measurement against the vendored binary. Do not "fix" either back.**

        * `-external=static` — resolve imports that are not in the tree from the module graph in
          the root `go.mod` **and nothing else**. The default mode shells out to `go get` /
          `go list` / `git ls-remote`, resolves in a throwaway temp module that ignores the
          union `go.mod`'s pins entirely, tries to reach the network for an internal import, and
          then **drops a real dependency while exiting 0** — a silent hole in the dependency
          graph, which is the one failure class a migration harness must never produce. Static
          mode used zero subprocesses, zero network, and resolved a strict superset.
        * `-index=all` — Gazelle's default, restated here so it cannot be removed by accident.
          `-index=none` makes the resolver **fabricate** labels for directories that do not
          exist and simultaneously **drop** real cross-repo edges; both failures are `exit 0`.

        Deliberately NOT the directory to run over and NOT `update -r`: which roots are visited
        is the driver's, because the invocation must cover **every** Go repo in the fleet at once
        (an import of a sibling resolves to `//go/digest` only when that root is on the same
        command line; with one root it is silently dropped) and one unit cannot know the others.
        Every Go unit therefore declares the identical flags, which is what lets the driver
        collapse them into one argv.
        """
        prefix = _module_path(unit)
        directives = [f"# gazelle:prefix {prefix}"] if prefix else []
        directives.append("# gazelle:go_naming_convention import")
        return GazelleConfig(
            directives=directives,
            prefix=prefix,
            exclude=["vendor", "third_party"],
            args=["-external=static", "-index=all"],
        )

    def toolchain_requirements(self) -> list[ToolchainRequirement]:
        """A pinned, downloaded Go SDK — `rules_go` otherwise adopts the host's `go`, and the
        version that compiles the fleet then depends on which machine ran the wave."""
        return [
            ToolchainRequirement(
                ruleset="rules_go",
                extension="go_sdk.download",
                name="go_sdk",
                version=_GO_VERSION,
                attrs={"version": _GO_VERSION},
            )
        ]


def _require_line(coordinate: Coordinate) -> str:
    """One `require` entry, or `GoModuleCoordinateError` naming the coordinate that cannot be one.

    Both halves are validated because `go` rejects both, at different moments and with different
    blast radii: a malformed module path fails at PARSE time, taking the whole file — every other
    repo's requirements included — with it, while a version `go` cannot fetch fails during
    resolution. Neither is a failure an operator can act on from `go`'s own message, because
    neither names the repo whose manifest produced the coordinate.

    A missing version is rejected rather than defaulted. The previous renderer wrote `v0.0.0` for
    it, which reads as a floor and is not one: `go` answers `invalid version: unknown revision
    v0.0.0` (measured, ADR-0050), so the default only moved the failure from here to the resolver
    and dropped the coordinate's identity on the way.
    """
    group = coordinate.group.strip("/")
    path = f"{group}/{coordinate.name}" if group else coordinate.name
    version = coordinate.version_spec or ""
    if not _MODULE_PATH_RE.fullmatch(path) or "." not in path.split("/", 1)[0]:
        raise GoModuleCoordinateError(
            f"{coordinate.key}: {path!r} is not a Go module path, so it cannot be a `require` "
            f"line in the monorepo's root go.mod — `go` rejects the whole file at parse time "
            f"(`malformed module path {path!r}: missing dot in first path element`), which would "
            f"take every other Go repo's requirements down with it"
        )
    if not _MODULE_VERSION_RE.fullmatch(version) or version == "v0.0.0":
        raise GoModuleCoordinateError(
            f"{coordinate.key}: {version or '<none>'!r} is not a Go module version for {path!r}, "
            f"so `require {path} {version or '<none>'}` is not a line this harness may write; a "
            f"version invented here would be a resolution, and `v0.0.0` is not a floor — `go` "
            f"answers `invalid version: unknown revision v0.0.0`"
        )
    return f"\t{path} {version}"


def _go_requires(units: Sequence[BuildUnit]) -> list[str]:
    """Every Go unit's requirements as ONE sorted, deduped `require` body (ADR-0050 step 2).

    Sorted over a set, so the union is a function of the fleet and not of the order the driver
    grouped the units in, and the same plan renders the same bytes in every process (§11.6).
    Deduped on the rendered LINE, not on the module path: two repos pinning the same module at
    different versions contribute two lines, and Go's MVS takes the higher — which is the Go
    toolchain's reconciliation, the only one `workspace_deps` permits.

    **Only `Ecosystem.GO` coordinates are eligible, and that is a type filter, not a drop.** A
    `BuildUnit`'s `external_coordinates` are re-read from *every* manifest the repo ships
    (`cli._external_coordinates` dispatches on the file, not on the unit's ecosystem), so a
    Go-primary repo that also has a `package.json` carries npm coordinates here. An npm package is
    not a Go module and can never be a `require` line in any rendering; including it is what wrote
    `left-pad ^1.3.0` into the root module file and made `go` refuse to parse it. Nothing about
    the Go module graph is lost by excluding it — the JS adapter's own root files are where that
    dependency is expressed. A coordinate that *claims* `Ecosystem.GO` and still cannot render is
    a different thing and is raised on, in `_require_line`.
    """
    return sorted(
        {
            _require_line(coordinate)
            for unit in units
            for coordinate in unit.external_coordinates
            if coordinate.ecosystem is Ecosystem.GO and coordinate.name
        }
    )


def _go_mod_text(requires: Sequence[str]) -> str:
    """The monorepo's root `go.mod` over an already-unioned `require` body.

    Takes the rendered lines rather than the units so that `workspace_files` and `resolution` are
    literally the same bytes rather than two renderings that agree until one is edited: the sums
    `go mod download all` writes are hashes taken against this exact file, and a resolver input
    that differs from what lands is a checksum mismatch inside Bazel that reads as a supply-chain
    compromise.

    `go <version>` is the pinned SDK (Gazelle also requires `>= 1.17`), read off the constant
    `go_sdk.download` registers so the file cannot ask for a toolchain Bazel will not fetch.
    """
    lines = [
        "// GENERATED BY fleet — the monorepo's own module over every Go repo's requirements.",
        f"module {_MONOREPO_MODULE}",
        "",
        f"go {_GO_VERSION}",
    ]
    if requires:
        lines += ["", "require (", *requires, ")"]
    return "\n".join(lines) + "\n"


def _module_path(unit: BuildUnit) -> str | None:
    """`<group>/<name>` of the unit's published coordinate — the Go module path, as written."""
    coordinate = unit.published
    if coordinate is None:
        return None
    group = coordinate.group.strip("/")
    return f"{group}/{coordinate.name}" if group else coordinate.name


def _bazel_repo_name(coordinate: Coordinate) -> str:
    """`github.com/acme/commons` → `com_github_acme_commons` — Gazelle's own repo-naming rule.

    Reversing the host (`github.com` → `com_github`) is not decoration: it is what `go_deps`
    generates, and a label this harness spells differently is a `no such repository` error at
    the point of use, long after the BUILD file that wrote it.
    """
    parts = [p for p in f"{coordinate.group}/{coordinate.name}".split("/") if p]
    if parts and "." in parts[0]:
        parts[0] = ".".join(reversed(parts[0].split(".")))
    slug = "_".join(parts)
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in slug.lower())
    return cleaned or "go_module"
