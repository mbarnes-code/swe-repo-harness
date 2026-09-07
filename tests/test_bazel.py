"""Behaviour tests for `src/fleet/bazel/` — §3.3 layout/emission and §3.4's rdeps closure.

**Read this before relaxing an assertion here.** Each test below pins a behaviour that a
specific, named earlier rule got wrong, and is written so that the earlier rule cannot be green:

* `test_mvs_refuses_the_plurality_winner` is the headline. The rule it replaces was "the highest
  version satisfying the **most** specs" — a plurality vote mislabelled as MVS. On the spec's own
  example (30 repos pinned `[31, 32)`, 31 repos pinned `[33, 34)`) that rule selects 33 and ships
  a build violating 30 repos' declared upper bound, silently. Real MVS finds the intersection
  empty and raises.
* `test_a_pin_that_hides_a_violation_is_rejected` pins the trust boundary: the
  `conflict_resolution` role proposes, code disposes. A pin whose damage is understated reaches
  a reviewer looking safe.
* `test_atomic_wave_emits_exactly_one_library_target` pins the coarsening at the *emission* end:
  one target per member reproduces the SCC in the Bazel target graph and the whole dependent
  cone is stranded behind a build that can never go green.
* `test_build_text_is_byte_stable_across_processes` runs real subprocesses under three
  `PYTHONHASHSEED`s. A `dict` populated from a `set` re-orders per process, which would rewrite
  every BUILD file in the monorepo on the next run and bury the real diff.
* `test_a_capped_closure_is_disclosed_not_silent` pins §3.4's two readings of one success
  criterion: the full closure under the bound, a *disclosed*, reproducible sample above it.

Generation is pure by construction, so no test here needs the `bazel` binary except the seven at
the bottom, which DO run it (`tools/bin/bazel` → bazelisk → Bazel 9.2.0, put on PATH by
`tests/conftest.py`): a real `build` of a generated package; a real MVS resolution of the
generated `bazel_dep` lines against the Bazel Central Registry; a real evaluation of the
generated `use_extension` blocks; a check that the version Bazel SELECTS for a ruleset is the
version `build.ruleset_versions` configured (it is `single_version_override`, not the `bazel_dep`
floor, that makes that true); a real `bazel build` of an adapter-generated `py_library`,
which is what holds its `srcs` to paths that resolve inside the package and to files
rules_python accepts; a real resolution of a `load()` whose ruleset ONLY a target names (D6); and
a real `mod show_repo` of the repos a toolchain extension creates, which exist for nobody until
`use_repo` imports them (D11). Five of those seven began as `xfail(strict=True)` or as a
reproduced failure on a live defect — the last two assert the real error FIRST, from the same
renderer call with the new input withheld, so neither can pass vacuously. See
`docs/INTEGRATION_HONESTY.md`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest

import fleet
from fleet import cli
from fleet.bazel.generators import (
    VersionConflict,
    coarse_build_targets,
    mvs_select,
    parse_range,
    reconcile_versions,
    render_build_bazel,
    render_gazelle_build,
    render_module_bazel,
    resolve_workspace_deps,
    stub_alias_target,
    stub_failing_target,
    validate_override,
)
from fleet.bazel.layout import (
    LayoutNode,
    PublishedCoordinate,
    ReservedDestError,
    is_reserved_dest,
    layout,
    normalize_dest,
    scc_label,
    select_primary_coordinate,
    skeleton_paths,
    stub_dest,
)
from fleet.bazel.lockfile import (
    LockfileRegistryMismatchError,
    check_lock_registry,
    lock_registry_urls,
)
from fleet.bazel.query import (
    BazelQueryError,
    RdepsClosure,
    bazel_test_argv,
    direct_rdeps_query,
    kind_rule_query,
    parse_target_labels,
    query_argv,
    query_stdout,
    rdeps_closure,
    rdeps_query,
    registry_args,
    select_tested_targets,
    write_target_pattern_file,
)

# `tests_query` is re-bound under a name that does not start with "test": a bare `from ... import
# tests_query` above would put a module-level name starting with "test" into THIS file, and
# pytest's default `python_functions` collects any such name as a test item — `tests_query` has
# no fixture named `dest` and errors at collection. Production code (never collected by pytest)
# keeps the SPEC-matching name; only this import site needs the alias.
from fleet.bazel.query import tests_query as bazel_tests_query
from fleet.ecosystems.base import ruleset_repo_names
from fleet.ecosystems.go import GoAdapter
from fleet.ecosystems.js import JsAdapter
from fleet.ecosystems.py import PyAdapter
from fleet.graph.build import build_graph
from fleet.graph.collisions import CollisionInput, DestClaim, VersionRequirement, audit_collisions
from fleet.graph.cycles import MemberSources, break_cycles, coarsen_atomic_scc
from fleet.models.build import (
    BuildTarget,
    BuildUnit,
    GazelleConfig,
    InternalDep,
    ToolchainRequirement,
    WorkspaceDep,
)
from fleet.models.enums import BreakStrategy, Ecosystem, EdgeKind, Equivalence, NodeKind
from fleet.models.graph import DependencyEdge, GraphNode, edge_key_for
from fleet.models.repo import Coordinate
from fleet.models.tasks import VerificationReport
from fleet.settings import BCR_DEFAULT_REGISTRY, BCR_MIRROR_REGISTRY, BuildSection
from fleet.util.proc import ProcResult
from tests.conftest import (
    BAZEL_REPOSITORY_CACHE,
    HTTP_TIMEOUT_SCALING,
    reap_bazel_state,
    tree_bytes,
)

REPO_ROOT = Path(fleet.__file__).resolve().parents[2]
RUN_ID = UUID("00000000-0000-4000-8000-000000000042")


# =======================================================================================
# fakes — a five-line registry is the whole point of the Protocol (guardrail 3)
# =======================================================================================


@dataclass(frozen=True)
class FakeAdapter:
    """The `LayoutAdapter` slice of an `EcosystemAdapter`. Names its own directory, so no
    language directory is spelled anywhere in `src/fleet/bazel/` (§13 row 33)."""

    monorepo_dir: str
    style: str = "group"

    def path_tail(self, coordinate: Coordinate) -> str:
        if self.style == "name":
            return coordinate.name
        return f"{coordinate.group_path}/{coordinate.name}"


@dataclass(frozen=True)
class FakeRegistry:
    adapters: dict[Ecosystem, FakeAdapter]

    def for_ecosystem(self, ecosystem: Ecosystem, /) -> FakeAdapter:
        return self.adapters[ecosystem]


def registry(jvm_dir: str = "jvm-root") -> FakeRegistry:
    return FakeRegistry(
        {
            Ecosystem.MAVEN: FakeAdapter(jvm_dir),
            Ecosystem.NPM: FakeAdapter("node-root"),
            Ecosystem.UNKNOWN: FakeAdapter("other-root", style="name"),
        }
    )


def coord(name: str, *, group: str = "com.acme", eco: Ecosystem = Ecosystem.MAVEN) -> Coordinate:
    return Coordinate(ecosystem=eco, group=group, name=name)


def edge(src: str, dst: str) -> DependencyEdge:
    dst_coord = coord(dst)
    return DependencyEdge(
        edge_key=edge_key_for(
            src_kind=NodeKind.REPO.value,
            src_id=src,
            dst_kind=NodeKind.REPO.value,
            dst_ref=dst_coord.key,
            kind=EdgeKind.DECLARED_DEP.value,
            evidence_path="pom.xml",
            evidence_line=None,
        ),
        src_id=src,
        dst_coordinate=dst_coord,
        dst_id=dst,
        kind=EdgeKind.DECLARED_DEP,
        base_confidence=1.0,
        confidence=1.0,
        evidence_path="pom.xml",
    )


def reqs(specs: dict[str, int], *, coord_key: str = "maven:com.acme:widget") -> list[
    VersionRequirement
]:
    """`{spec: how many repos declare it}` → one `VersionRequirement` per repo."""
    out: list[VersionRequirement] = []
    index = 0
    for spec, count in sorted(specs.items()):
        for _ in range(count):
            out.append(
                VersionRequirement(coord_key=coord_key, repo_id=f"repo-{index:03d}",
                                   version_spec=spec)
            )
            index += 1
    return out


@dataclass(frozen=True)
class Proposal:
    """A `VersionConflictResolution` structurally (§7.7) — the validator must not need the LLM
    package to validate the LLM."""

    coord_key: str
    proposed_version: str
    mechanism: str = "single_version_override"
    violated_specs: tuple[str, ...] = ()


# =======================================================================================
# §3.3 — layout
# =======================================================================================


def test_layout_is_adapter_derived_not_hardcoded() -> None:
    """§13 row 33: re-pointing one adapter's `monorepo_dir` moves every `dest` and `//` label
    with zero source edits. If `layout.py` knew a directory name, this test could not pass."""
    node = LayoutNode(node_id="acme-widget", ecosystem=Ecosystem.MAVEN, published=coord("widget"))
    assert layout(node, registry()) == "jvm-root/com/acme/widget"
    assert layout(node, registry(jvm_dir="java")) == "java/com/acme/widget"


def test_a_node_with_no_published_coordinate_is_not_a_special_case() -> None:
    """§3.1 step 2 / §3.3: no primary published coordinate ⇒ the UNKNOWN adapter's
    `<dir>/<repo_id>`. `layout()` is total *because the registry is*, not because it carries a
    fallback branch — a `None` coordinate takes the same registry lookup as every other node."""
    node = LayoutNode(node_id="mystery-repo", ecosystem=Ecosystem.UNKNOWN)
    assert layout(node, registry()) == "other-root/mystery-repo"


def test_a_contract_node_uses_its_persisted_hoist_target_path() -> None:
    """§3.3: `layout()` is total over graph *nodes*. A contract's dest is
    `contracts.hoist_target_path`, computed once in §3.1 5b and persisted — recomputing it in
    Phase 3 is how two modules come to disagree about where history was merged."""
    node = LayoutNode(
        node_id="proto:acme.billing",
        ecosystem=Ecosystem.MAVEN,
        hoist_target_path="proto/acme/billing/",
    )
    assert layout(node, registry()) == "proto/acme/billing"


def test_an_operator_dest_override_wins() -> None:
    """§3.3: "`layout(repo)` is `dest_override` from `config/repos.yaml` if present"."""
    node = LayoutNode(
        node_id="acme-widget",
        ecosystem=Ecosystem.MAVEN,
        published=coord("widget"),
        dest_override="legacy/widget",
    )
    assert layout(node, registry()) == "legacy/widget"


def test_scc_is_a_reserved_path_segment_under_every_monorepo_dir() -> None:
    """§3.3: "`_scc` is reserved" — a repo landing there would shadow the union target §3.1 6e
    emits into that package, so the SCC's own sources and its coarsened target would collide in
    a tree that has already been merged. `layout()` refuses to return such a dest at all, and
    the Phase 1 step-8 audit is where it becomes a recorded, resolvable collision."""
    node = LayoutNode(
        node_id="acme-widget",
        ecosystem=Ecosystem.MAVEN,
        published=coord("widget"),
        dest_override="jvm-root/_scc/17",
    )
    with pytest.raises(ReservedDestError) as excinfo:
        layout(node, registry())
    assert "_scc" in str(excinfo.value)

    assert is_reserved_dest("java/_scc/scc_00ff")
    assert not is_reserved_dest("java/com/acme/_scc_helper")

    report = audit_collisions(
        CollisionInput(dests=[DestClaim(node_id="acme-widget", dest="jvm-root/_scc/17")])
    )
    assert report.blocking or report.dest_rewrites, "step 8 must record it, not shrug"


def test_the_scc_label_is_a_legal_bazel_label() -> None:
    """`scc_id` is `'scc:<16hex>'`; `//pkg:scc:abc…` does not parse, because `:` is the label
    separator. `scc_target_name` folds it, and the label must show that folding survived."""
    label = scc_label(Ecosystem.MAVEN, "scc:00ff11ee22dd33cc", registry())
    package, _, name = label.rpartition(":")
    assert package == "//jvm-root/_scc/scc_00ff11ee22dd33cc"
    assert name == "scc_00ff11ee22dd33cc"
    assert label.count(":") == 1, "exactly one ':' — the label separator"


def test_the_primary_coordinate_is_the_one_with_most_inbound_edges() -> None:
    """§3.3: ties break on `Coordinate.key`, never on input order — a `dest` that depends on the
    order SQLite returned rows is a `dest` that moves between runs (§11.6)."""
    a, b, c = coord("alpha"), coord("beta"), coord("gamma")
    chosen = select_primary_coordinate(
        [PublishedCoordinate(a, 3), PublishedCoordinate(b, 9), PublishedCoordinate(c, 9)]
    )
    assert chosen is not None and chosen.name == "beta"
    assert select_primary_coordinate([]) is None


def test_dest_normalization_refuses_to_escape_the_monorepo() -> None:
    """`dest` is joined into the integration worktree in Phase 3 step 1: a `..` reaching it is a
    write outside the monorepo, not a cosmetic path issue."""
    assert normalize_dest("java//com/acme/") == "java/com/acme"
    for bad in ("/abs/path", "java/../../etc", ""):
        with pytest.raises(ValueError):
            normalize_dest(bad)


def test_the_skeleton_names_no_language_directory() -> None:
    """§3.3: the fixed skeleton is pipeline-owned; every language directory under it is
    `adapter.monorepo_dir` and "not a constant in this document"."""
    paths = skeleton_paths()
    assert "MODULE.bazel" in paths and "third_party/stubs" in paths
    assert not {"java", "ts", "py", "go", "rust", "misc"} & set(paths)


# =======================================================================================
# §3.3 step 3 — MVS version reconciliation
# =======================================================================================


def test_mvs_picks_the_minimum_version_satisfying_all_specs() -> None:
    """Bazel's MVS is the *maximum of the declared lower bounds* — the SMALLEST version that can
    satisfy every spec. Not the newest that fits: selecting 34 here would silently upgrade every
    repo that asked for 31, which is the behaviour bzlmod exists to avoid."""
    requirements = reqs({">=31": 5, ">=33,<35": 2, ">=32": 1})
    assert mvs_select("maven:com.acme:widget", requirements) == "33"


def test_mvs_refuses_the_plurality_winner() -> None:
    """THE regression guard (§3.3 step 3). The rule this replaces — "the highest version
    satisfying the MOST specs" — selects 33 here, because 31 repos pin `[33, 34)` and only 30 pin
    `[31, 32)`, and ships a build violating those 30 repos' declared upper bound with no finding
    at all. Real MVS finds the intersection EMPTY: max lower bound 33 > min upper bound 32.

    This test is red under the plurality rule by construction — that rule returns a version, and
    the only correct answer is a refusal.
    """
    requirements = reqs({">=31,<32": 30, ">=33,<34": 31})
    with pytest.raises(VersionConflict) as excinfo:
        mvs_select("maven:com.acme:widget", requirements)
    conflict = excinfo.value
    assert conflict.severity == "error", "an unsatisfiable set blocks; it is never a warning"
    assert "intersection is empty" in conflict.reason
    assert set(conflict.specs) == {">=31,<32", ">=33,<34"}
    assert len(conflict.repo_ids) == 61
    assert conflict.as_finding().kind == "VersionConflict"


def test_reconcile_collects_every_conflict_and_still_resolves_the_rest() -> None:
    """One unsatisfiable coordinate must not hide the other four in the same wave: the operator
    wants the whole list before `conflict_resolution` is invoked even once (an LLM call per wave
    boundary is real money). `require_resolved()` is the fail-loud edge (Rule 11)."""
    requirements = [
        *reqs({">=31,<32": 1, ">=33,<34": 1}, coord_key="maven:com.acme:widget"),
        *reqs({">=2.0": 1, ">=2.4,<3": 1}, coord_key="maven:com.acme:gadget"),
    ]
    resolution = reconcile_versions(requirements)
    assert resolution.selected == {"maven:com.acme:gadget": "2.4"}
    assert [c.coord_key for c in resolution.conflicts] == ["maven:com.acme:widget"]
    assert not resolution.ok
    assert [f.severity for f in resolution.findings] == ["error"]
    with pytest.raises(VersionConflict):
        resolution.require_resolved()


def test_a_satisfiable_set_never_reaches_the_model() -> None:
    """§3.3 step 3: MVS decides every satisfiable case without a model. The `conflict_resolution`
    role is reachable only from an empty intersection — that gate is the cost control."""
    resolution = reconcile_versions(reqs({"^1.2.0": 4, ">=1.4": 3, "~1.4.2": 2}))
    assert resolution.ok and resolution.conflicts == ()
    assert resolution.selected == {"maven:com.acme:widget": "1.4.2"}


def test_a_pin_that_hides_a_violation_is_rejected() -> None:
    """§7.7: the model's suggestion is a *proposal*. Code re-derives which specs the pin actually
    breaks and rejects anything the model failed to disclose — an understated pin is the one that
    reaches a reviewer looking safe. The proposal is validated BEFORE anything is written."""
    requirements = reqs({">=31,<32": 30, ">=33,<34": 31})
    hiding = Proposal(coord_key="maven:com.acme:widget", proposed_version="33")

    decision = validate_override(hiding, requirements)
    assert not decision.accepted
    assert decision.violated == (">=31,<32",)
    assert decision.undisclosed == (">=31,<32",)
    assert "did not disclose" in decision.reason


def test_a_pin_that_discloses_every_violation_is_accepted() -> None:
    """The counterpart: on an empty intersection EVERY pin breaks someone's bound, so "violates a
    spec" cannot be the rejection rule — disclosure is. Otherwise no override could ever be
    written and the run would deadlock on a conflict it invoked the model to resolve."""
    requirements = reqs({">=31,<32": 30, ">=33,<34": 31})
    honest = Proposal(
        coord_key="maven:com.acme:widget", proposed_version="33", violated_specs=(">=31,<32",)
    )
    decision = validate_override(honest, requirements)
    assert decision.accepted and decision.violated == (">=31,<32",)


def test_an_override_for_an_unknown_mechanism_or_coordinate_is_rejected() -> None:
    """`mechanism` and `coord_key` are model-written strings that would otherwise be interpolated
    straight into MODULE.bazel. A closed set is checked here, not trusted upstream."""
    requirements = reqs({">=31,<32": 1, ">=33,<34": 1})
    assert not validate_override(
        Proposal(
            "maven:com.acme:widget", "33", mechanism="vendor_it",
            violated_specs=(">=31,<32",),
        ),
        requirements,
    ).accepted
    assert not validate_override(Proposal("maven:com.acme:other", "33"), requirements).accepted
    assert not validate_override(
        Proposal("maven:com.acme:widget", "latest", violated_specs=(">=31,<32",)), requirements
    ).accepted


def test_an_unparseable_spec_never_produces_a_conflict_it_cannot_prove() -> None:
    """A `VersionConflict` at error severity blocks the run, so it must never rest on a parse the
    module was not sure of — the unparseable spec is excluded from the bounds, not guessed at."""
    assert parse_range("${env.WIDGET_VERSION}") is None
    resolution = reconcile_versions(reqs({">=31": 1, "${env.V}": 1}))
    assert resolution.ok and resolution.selected == {"maven:com.acme:widget": "31"}


# =======================================================================================
# §3.3 step 2 — BUILD generation
# =======================================================================================


def targets_fixture() -> list[BuildTarget]:
    return [
        BuildTarget(
            package="jvm-root/com/acme/widget",
            name="widget",
            rule="java_library",
            load_from="@rules_java//java:defs.bzl",
            srcs=["src/Widget.java"],
            deps=["//node-root/acme/ui:ui", "//jvm-root/com/acme/base:base"],
            attrs={"javacopts": ["-Xlint:all"]},
        ),
        BuildTarget(
            package="jvm-root/com/acme/widget",
            name="widget_test",
            rule="java_test",
            load_from="@rules_java//java:defs.bzl",
            srcs=["test/WidgetTest.java"],
            deps=[":widget"],
            testonly=True,
        ),
    ]


def test_generated_build_text_is_deterministic_and_sorted() -> None:
    """§11.6: every ordering is an explicit `sorted`. Two runs over the same plan must produce
    identical bytes, or the integration branch shows a diff where nothing changed."""
    text = render_build_bazel(targets_fixture())
    assert text == render_build_bazel(list(reversed(targets_fixture())))
    assert text.index('name = "widget"') < text.index('name = "widget_test"')
    assert text.index('"//jvm-root/com/acme/base:base"') < text.index('"//node-root/acme/ui:ui"')
    assert text.count("load(") == 1, "one load per .bzl, symbols merged and sorted"
    assert 'load("@rules_java//java:defs.bzl", "java_library", "java_test")' in text
    assert "testonly = True" in text
    assert text.endswith("\n")


def test_build_text_is_byte_stable_across_processes() -> None:
    """Byte-stability across processes, not merely within one. A `dict` built from a `set`
    re-orders under a different `PYTHONHASHSEED`; the failure mode is 250 BUILD files rewritten
    on every run, which buries the one real diff a reviewer needed to see."""
    script = (
        "import sys, hashlib;"
        f"sys.path.insert(0, {str(REPO_ROOT)!r});"
        "from tests.test_bazel import targets_fixture;"
        "from fleet.bazel.generators import render_build_bazel;"
        "print(hashlib.sha256(render_build_bazel(targets_fixture()).encode()).hexdigest())"
    )
    digests = set()
    for seed in ("0", "1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(REPO_ROOT / "src")}
        out = subprocess.run(  # noqa: S603
            [sys.executable, "-c", script],
            check=True, capture_output=True, text=True, cwd=REPO_ROOT, env=env,
        )
        digests.add(out.stdout.strip())
    assert len(digests) == 1, f"BUILD text differs by hash seed: {digests}"


def test_a_build_file_describes_exactly_one_package() -> None:
    """Rule 11: two packages in one BUILD file is a driver bug, and the labels it produces would
    point at targets that do not exist. Loud, not merged."""
    mixed = [*targets_fixture(), targets_fixture()[0].model_copy(update={"package": "elsewhere"})]
    with pytest.raises(ValueError, match="one BUILD file describes exactly one package"):
        render_build_bazel(mixed)


def test_a_gazelle_adapter_emits_directives_not_targets() -> None:
    """§3.3 step 2: `uses_gazelle` delegates emission — "it does not pretend to generate targets
    itself". The preamble is written, then Gazelle writes the targets."""
    text = render_gazelle_build(
        GazelleConfig(directives=["# gazelle:go_naming_convention import"],
                      prefix="github.com/acme/svc", exclude=["vendor"])
    )
    assert "# gazelle:prefix github.com/acme/svc" in text
    assert "# gazelle:exclude vendor" in text
    assert "_library(" not in text and "_binary(" not in text


def test_atomic_wave_emits_exactly_one_library_target() -> None:
    """§3.1 6e / §3.3: ONE library target per `(ecosystem, scc_id)`, whose `srcs` are the UNION
    of the members' sources. One target per member reproduces the cycle in the Bazel target
    graph, `bazel build` fails with a dependency-cycle error, and every repo in the SCC's
    dependent cone is stranded behind a build that can never go green.
    """
    graph = build_graph(
        [GraphNode(kind=NodeKind.REPO, node_id=r) for r in ("acme-a", "acme-b", "acme-c")],
        [edge("acme-a", "acme-b"), edge("acme-b", "acme-c"), edge("acme-c", "acme-a")],
    )
    report = break_cycles(graph)
    resolution = report.resolutions[0]
    assert resolution.break_strategy is BreakStrategy.ATOMIC_WAVE
    members = [
        MemberSources(repo_id=r, ecosystem=Ecosystem.MAVEN, dest=f"jvm-root/{r}",
                      srcs=("Main.java",))
        for r in resolution.members
    ]
    plan = coarsen_atomic_scc(
        resolution, members, report.graph, monorepo_dirs={Ecosystem.MAVEN: "jvm-root"}
    )

    built = coarse_build_targets(plan, {Ecosystem.MAVEN: "java_library"})
    assert len(built) == 1, "one target per (ecosystem, scc_id), never one per member"
    target = built[0]
    assert target.srcs == [
        "jvm-root/acme-a/Main.java", "jvm-root/acme-b/Main.java", "jvm-root/acme-c/Main.java",
    ], "the union of member sources — that union IS the acyclic unit"
    assert ":" not in target.name, "':' is the label separator, not a target-name character"
    assert target.label == f"//jvm-root/_scc/{target.name}:{target.name}"
    assert target.package.split("/")[1] == "_scc"
    render_build_bazel(built)  # the label survives rendering


def test_coarsening_without_a_rule_for_the_ecosystem_fails_loud() -> None:
    """Rule 11: an SCC that cannot be coarsened must not silently emit zero targets — that is a
    green Phase 3 for a package containing none of the SCC's code."""
    graph = build_graph(
        [GraphNode(kind=NodeKind.REPO, node_id=r) for r in ("acme-a", "acme-b")],
        [edge("acme-a", "acme-b"), edge("acme-b", "acme-a")],
    )
    report = break_cycles(graph)
    plan = coarsen_atomic_scc(
        report.resolutions[0],
        [MemberSources(repo_id=r, ecosystem=Ecosystem.MAVEN, dest=f"jvm-root/{r}", srcs=("M.java",))
         for r in report.resolutions[0].members],
        report.graph,
        monorepo_dirs={Ecosystem.MAVEN: "jvm-root"},
    )
    with pytest.raises(ValueError, match="no library rule registered"):
        coarse_build_targets(plan, {})


# =======================================================================================
# §3.3 step 3 — MODULE.bazel
# =======================================================================================


def widget_dep(version: str | None = "31") -> WorkspaceDep:
    return WorkspaceDep(
        ruleset="rules_jvm_external",
        extension="maven.install",
        coordinate=coord("widget"),
        resolved_version=version,
        repo_name="maven",
        attrs={"artifacts": [f"com.acme:widget:{version}"]},
    )


def test_module_bazel_records_every_requirement_as_a_bazel_dep() -> None:
    """§3.3 step 3: reconciliation is MVS over `WorkspaceDep`s and is ecosystem-free. Rulesets
    are pinned by the operator (`build.ruleset_versions`, §9); the adapter names its ruleset."""
    text = render_module_bazel(
        [widget_dep()],
        module_name="acme-monorepo",
        ruleset_versions={"rules_jvm_external": "6.7", "rules_python": "1.0.0"},
        toolchains=[
            ToolchainRequirement(ruleset="rules_python", extension="python.toolchain",
                                 name="python_3_12", version="3.12")
        ],
        single_version_overrides={"rules_jvm_external": "6.7"},
    )
    assert 'bazel_dep(name = "rules_jvm_external", version = "6.7")' in text
    assert 'single_version_override(module_name = "rules_jvm_external", version = "6.7")' in text
    assert 'maven = use_extension("@rules_jvm_external//:extensions.bzl", "maven")' in text
    assert "maven.install(" in text
    assert 'use_repo(maven, "maven")' in text
    assert "python.toolchain(" in text
    assert text == render_module_bazel(
        [widget_dep()],
        module_name="acme-monorepo",
        ruleset_versions={"rules_jvm_external": "6.7", "rules_python": "1.0.0"},
        toolchains=[
            ToolchainRequirement(ruleset="rules_python", extension="python.toolchain",
                                 name="python_3_12", version="3.12")
        ],
        single_version_overrides={"rules_jvm_external": "6.7"},
    ), "rendering is a pure function of its inputs"


def test_an_unpinned_ruleset_is_refused() -> None:
    """§9: an unpinned `bazel_dep` resolves differently on the next run, which makes a green
    build unreproducible — the one property Phase 3 exists to establish."""
    with pytest.raises(ValueError, match="pins no version"):
        render_module_bazel([widget_dep()], module_name="m", ruleset_versions={})


def test_mvs_output_flows_into_the_rendered_deps() -> None:
    """The join between step 3's reconciliation and the file it writes: `resolved_version` is
    MVS's winner, and a dep that never went through reconciliation must not be renderable."""
    resolved = resolve_workspace_deps(
        [widget_dep(version=None).model_copy(update={"attrs": {}})],
        {"maven:com.acme:widget": "33"},
    )
    assert resolved[0].resolved_version == "33"
    with pytest.raises(ValueError, match="no resolved_version"):
        render_module_bazel(
            [widget_dep(version=None).model_copy(update={"attrs": {}})],
            module_name="m",
            ruleset_versions={"rules_jvm_external": "6.7"},
        )


def test_a_ruleset_nothing_uses_is_never_emitted() -> None:
    """`bazel_dep` follows USE, never the configuration table (D6's fix must not overshoot).

    **Why:** `build.ruleset_versions` is a pin list, not a dependency list — it names every
    ruleset the fleet *could* need. Emitting a `bazel_dep` per pinned entry would make every
    monorepo, however monolingual, resolve and fetch `rules_go`, `rules_rust` and `rules_proto`
    from BCR, and would make an unreachable registry or a yanked version break a build that never
    used the ruleset. A native `filegroup` carries no `load_from`, so it claims nothing.
    """
    text = render_module_bazel(
        [],
        module_name="m",
        ruleset_versions=dict(BuildSection().ruleset_versions),
        targets=[BuildTarget(package="", name="all_srcs", rule="filegroup", srcs=["MODULE.bazel"])],
    )
    assert "bazel_dep(" not in text, text
    assert "single_version_override(" not in text, text


def test_a_load_from_an_apparent_repo_that_is_not_a_pinned_ruleset_is_left_alone() -> None:
    """A `load()` label names an APPARENT REPO, which is not always a module name — and the
    difference is not ours to guess.

    **Why:** `rules_go` publishes itself as `@io_bazel_rules_go` (`module(name = "rules_go",
    repo_name = "io_bazel_rules_go")`), so `bazel_dep(name = "rules_go")` is what puts
    `@io_bazel_rules_go//go:def.bzl` in scope and `bazel_dep(name = "io_bazel_rules_go")` names
    nothing in any registry. Promoting every load label to a `bazel_dep` — or raising "pins no
    version" on one — would break every Go repo in the fleet with an error invented here. The
    operator's pin in `build.ruleset_versions` is the statement that a name is a module; absent
    it, the label is somebody else's `repo_name` and is passed through untouched.
    """
    text = render_module_bazel(
        [],
        module_name="m",
        ruleset_versions={"rules_go": "0.50.1"},
        targets=[
            BuildTarget(
                package="go/acme",
                name="acme",
                rule="go_library",
                load_from="@io_bazel_rules_go//go:def.bzl",
            )
        ],
    )
    assert "io_bazel_rules_go" not in text, text
    assert "bazel_dep(" not in text, "the pinned rules_go is not USED by this plan either"


def test_a_ruleset_whose_apparent_name_differs_carries_it_as_repo_name() -> None:
    """The `bazel_dep` is what makes a ruleset's own labels resolvable, and for `rules_go` that
    takes `repo_name = "io_bazel_rules_go"`.

    **Why:** an apparent name is set by the DEPENDENT — the `module(repo_name = …)` in rules_go's
    own `MODULE.bazel` says only how it sees itself — so a `bazel_dep(name = "rules_go")` alone
    puts the module in scope as `@rules_go` and NOTHING as `@io_bazel_rules_go`. Every Go package
    Gazelle generates opens `load("@io_bazel_rules_go//go:def.bzl", …)`, so real Bazel answered
    `No repository visible as '@io_bazel_rules_go' from main repository` and refused to load the
    `go/` directory at all — the whole ecosystem, before analysis. Knowing the two names differ
    (the test above) kept a bogus `bazel_dep(name = "io_bazel_rules_go")` out of the file; only
    this emits the name the loads need. The map is the adapters', for `extension_bzl`'s reason.
    """
    text = render_module_bazel(
        [],
        module_name="m",
        ruleset_versions={"rules_go": "0.61.1", "rules_python": "1.0.0"},
        toolchains=[
            ToolchainRequirement(
                ruleset="rules_go", extension="go_sdk.download", name="go_sdk", version="1.23.4"
            ),
            ToolchainRequirement(
                ruleset="rules_python",
                extension="python.toolchain",
                name="python_3_12",
                version="3.12",
            ),
        ],
        extension_bzl={
            "go_sdk": "@io_bazel_rules_go//go:extensions.bzl",
            "python": "@rules_python//python/extensions:python.bzl",
        },
        ruleset_repo_names={"rules_go": "io_bazel_rules_go"},
    )
    assert (
        'bazel_dep(name = "rules_go", version = "0.61.1", repo_name = "io_bazel_rules_go")' in text
    ), text
    # …and nothing else gets one: a `repo_name` equal to the module name is noise, and a module
    # spelled two ways in one file is the failure this attribute exists to prevent.
    assert 'bazel_dep(name = "rules_python", version = "1.0.0")' in text, text
    assert "@rules_go//" not in text, text


def test_the_apparent_name_table_is_the_adapters_own_and_go_is_in_it() -> None:
    """The default for `ruleset_repo_names` is the registry, so the Go monorepo the pipeline
    renders carries the `repo_name` without any caller passing anything.

    **Why:** `render_module_bazel`'s callers (`workers/buildgen.py`, and every test that renders a
    fleet's module file) pass neither `extension_bzl` nor this map. A table that only an explicit
    argument could reach would be dead in the one path that matters, which is exactly how the
    `@rules_go` / `@io_bazel_rules_go` split survived until real Bazel was asked.
    """
    assert ruleset_repo_names()["rules_go"] == "io_bazel_rules_go"
    text = render_module_bazel(
        [],
        module_name="m",
        ruleset_versions=dict(BuildSection().ruleset_versions),
        toolchains=GoAdapter().toolchain_requirements(),
    )
    assert 'repo_name = "io_bazel_rules_go")' in text, text
    assert 'use_extension("@io_bazel_rules_go//go:extensions.bzl", "go_sdk")' in text, text


def test_the_root_module_states_no_go_deps_posture_so_gazelles_default_governs_d19() -> None:
    """**D19's policy decision, pinned as a negative so that changing it is a visible diff.**

    `go_deps.config(check_direct_dependencies = …)` is a **root-module-only** tag with values
    `off` / `warning` / `error`. The harness renders the root `MODULE.bazel` and therefore *could*
    emit it; this test asserts that it **does not**, which leaves gazelle's own initialiser
    (`outdated_direct_dep_printer = print`, i.e. `"warning"`) in charge. That is a decision, not
    an oversight, and these are the measurements it rests on — all three taken against the pinned
    gazelle 0.52.2 and Bazel 9.2.0, none of them read out of documentation:

    * With `= "error"` text-edited into this same file, the two-repo Go fixture goes from green to
      `Error in fail: The following Go modules were required by the root module at the given
      versions, but were implicitly updated to higher versions due to transitive dependencies:
      golang.org/x/crypto: v0.31.0 -> v0.39.0 / golang.org/x/sys: v0.28.0 -> v0.33.0`, aborting
      the **whole monorepo** at extension-evaluation time, before a single target is built.
    * The condition it reports is *inherent to Bzlmod* — `go_deps` is ONE extension over the whole
      module graph, rules_go and gazelle each call `from_file` on their own `go.mod`, and one Go
      module path gets exactly one repository — so **the harness cannot repair it**, and neither
      can any single repo in the fleet: the raise is a property of the pinned rulesets' floors.
      A fleet-wide hard failure on a condition nobody in the fleet caused and nobody in the fleet
      can fix is the wrong posture for a migration harness, and §25's corpus survey (25 Go repos,
      68 `go.mod` files) makes it near-certain that at least one repo pins below some floor.
    * It would **not** buy any checksum safety, which was the other reason to want it. Gazelle
      fails closed on a missing sum entirely independently of this setting — measured, and pinned
      by `test_a_go_module_with_no_sum_anywhere_in_the_graph_is_refused_rather_than_fetched`.

    So the harness stays silent-by-gazelle's-default and the residual is written down rather than
    engineered around; see that test's docstring for the residual risk in full.
    """
    text = render_module_bazel(
        [],
        module_name="m",
        ruleset_versions=dict(BuildSection().ruleset_versions),
        toolchains=GoAdapter().toolchain_requirements(),
    )
    assert "go_deps.config" not in text, text
    assert "check_direct_dependencies" not in text, text


# =======================================================================================
# §3.4 — the rdeps closure and its disclosed sample
# =======================================================================================


def test_the_affected_only_query_is_intersected_with_this_repos_rules() -> None:
    """§3.4 bounds table: verification is affected-targets-only. 250 repos × 4 phases × 3
    attempts is ~3 000 invocations; a full `//...` build at each is the cost explosion that ends
    the run. `affected_only=false` restores the full closure for a final gate run."""
    assert kind_rule_query("java/acme") == "kind(rule, //java/acme/...)"
    assert rdeps_query("java/acme") == "rdeps(//..., set(kind(rule, //java/acme/...)))"
    assert rdeps_query("java/acme", affected_only=False) == "rdeps(//..., //java/acme/...)"
    assert direct_rdeps_query("java/acme").endswith(", 1)")


def test_the_tests_query_mirrors_kind_rule_querys_shape() -> None:
    """§12.11's test-count comparison: `tests(//<dest>/...)`, one line, no state — the same shape
    as `kind_rule_query`, including the leading/trailing `/` strip so a `dest:` with either has
    the same behaviour a repo without one does."""
    assert bazel_tests_query("java/acme") == "tests(//java/acme/...)"
    assert bazel_tests_query("/java/acme/") == "tests(//java/acme/...)"


def test_the_test_invocation_carries_keep_going_and_the_bep(tmp_path: Path) -> None:
    """§3.4: `--keep_going` (one broken target must not hide the other nineteen),
    `--build_event_json_file` (so `FailureClass` comes from the BEP rather than a regex over
    stderr), and a `--target_pattern_file` rather than an argv explosion."""
    pattern_file = write_target_pattern_file(("//a:a", "//b:b"), tmp_path / "targets.txt")
    argv = bazel_test_argv(
        pattern_file=pattern_file, build_event_json_file=tmp_path / "bep.json", jobs=4
    )
    assert argv[0:2] == ("bazel", "test")
    assert f"--target_pattern_file={pattern_file}" in argv
    assert "--keep_going" in argv and "--jobs=4" in argv
    assert any(a.startswith("--build_event_json_file=") for a in argv)
    assert pattern_file.read_text(encoding="utf-8") == "//a:a\n//b:b\n"


def test_the_configured_registry_reaches_every_bazel_command_line(tmp_path: Path) -> None:
    """`build.registry` must appear as `--registry=` in the argv the harness actually runs.

    **Why:** `bcr.bazel.build` is unreachable from some networks — this one included, where it
    accepts the socket and stalls until Bazel's own timeout. On such a host the pins in
    `build.ruleset_versions` resolve nowhere, so *every* repo in the fleet fails Phase 3 with a
    message naming a ruleset. The setting exists to point resolution somewhere reachable, and it
    only means anything if it survives the trip into argv: a `registry` field that is read into a
    config object and never emitted is worse than no field, because the operator believes the
    fleet is resolving against the mirror they chose.

    Query and test are asserted together because they must agree — a closure computed against one
    module graph and tested against another is a blast radius measured on a different build.
    """
    mirror = "https://mirror.invalid/bcr"
    pattern_file = write_target_pattern_file(("//a:a",), tmp_path / "targets.txt")

    assert f"--registry={mirror}" in query_argv("//...", registry=mirror)
    assert f"--registry={mirror}" in bazel_test_argv(
        pattern_file=pattern_file, registry=mirror
    )

    # Unset means unset: `--registry` REPLACES Bazel's built-in list rather than adding to it, so
    # emitting a default here would narrow resolution for every operator who never set the key.
    assert not any(a.startswith("--registry") for a in query_argv("//...")), query_argv("//...")
    assert registry_args(None) == () and registry_args("") == ()
    assert BuildSection().registry is None

    # The flag precedes `extra`, so an `extra_args` registry is a FALLBACK behind the configured
    # one rather than something that silently outranks it.
    argv = query_argv("//...", registry=mirror, extra=("--registry=https://second.invalid",))
    assert argv.index(f"--registry={mirror}") < argv.index("--registry=https://second.invalid")


async def test_the_repository_cache_rides_on_both_rdeps_queries_and_the_disk_cache_does_not(
) -> None:
    """`bazel query` loads the module graph, so it wants the SAME repository cache the phase's
    `bazel test` was given — and wants nothing to do with the disk cache.

    Both flags parse for `query` on the vendored 9.2.0 (`canonicalize-flags --for_command=query --
    --disk_cache=/a --repository_cache=/r` echoes both back), so this is not about what Bazel
    would reject. It is about what it measurably USES: the same probe query wrote 2.3 MB into the
    repository cache and zero files into the disk cache, because `query` executes no actions and
    the disk cache holds nothing but actions and their outputs. `RdepverifyWorker` therefore hands
    this argv the repository flag only; `query_argv` emits what it is handed, in order.

    Asserted on BOTH queries because the depth-1 query — the one that runs precisely when a repo
    is over `rdeps_limit` — reloads the graph the first query just fetched. A cache named on one
    and not the other pays that fetch twice on the widest repos in the fleet.
    """
    flags = ("--repository_cache=/host/cache/repo",)

    argv = query_argv("//...", cache_flags=flags, extra=("--repository_cache=/operator",))
    assert argv[0:2] == ("bazel", "query")
    assert "--repository_cache=/host/cache/repo" in argv
    assert not any(a.startswith("--disk_cache=") for a in argv), argv
    # Cache flags first, `extra` last — Bazel keeps the LAST occurrence of a non-`allowMultiple`
    # option, so an operator's hand-written value must sit behind ours, not in front of it.
    assert argv.index("--repository_cache=/host/cache/repo") < argv.index(
        "--repository_cache=/operator"
    )
    # Nothing handed in, nothing emitted: a `--repository_cache=` naming a directory nobody
    # created is a cache that looks configured and is written where no one reads.
    assert not any(a.startswith("--repository_cache=") for a in query_argv("//..."))

    calls: list[tuple[str, ...]] = []

    async def runner(argv, *, cwd=None, env=None, deadline=None, timeout_s=None):
        calls.append(tuple(argv))
        body = "".join(f"//pkg{i}:lib\n" for i in range(9))
        return ProcResult(argv=tuple(argv), exit_code=0, stdout_tail=body, stderr_tail="",
                          duration_ms=1, timed_out=False)

    closure = await rdeps_closure(
        "java/acme", runner=runner, limit=4, sample_n=2, cache_flags=flags
    )
    assert closure.truncated and len(calls) == 2, calls
    assert all("--repository_cache=/host/cache/repo" in call for call in calls), calls
    assert not any(a.startswith("--disk_cache=") for call in calls for a in call), calls


def test_a_closure_under_the_bound_is_tested_whole() -> None:
    """§3.4's success criterion, first reading: the verified target set is the FULL rdeps closure
    when `rdeps_truncated` is false."""
    labels = [f"//pkg{i}:lib" for i in range(50)]
    closure = select_tested_targets(labels, query="q", limit=100, sample_n=5)
    assert closure.tested == tuple(sorted(labels))
    assert closure.total_count == 50
    assert not closure.truncated
    assert closure.equivalence is Equivalence.FULL


def test_a_capped_closure_is_disclosed_not_silent() -> None:
    """§3.4's second reading. Over `rdeps_limit` the tested set is all DIRECT rdeps plus
    `rdeps_sample_n` of the remainder by stable hash — and `rdeps_truncated` says so. The two
    readings are one criterion because the reduction is declared: a repo whose closure exceeds
    the limit must neither stall forever nor report 40 000 untested targets as green."""
    labels = [f"//pkg{i:05d}:lib" for i in range(3000)]
    direct = labels[:12]
    closure = select_tested_targets(
        labels, query="rdeps(//..., set(kind(rule, //java/acme/...)))",
        direct=direct, limit=2000, sample_n=500,
    )
    assert closure.truncated
    assert closure.total_count == 3000
    assert closure.sample_n == 500
    assert len(closure.tested) == 512, "every direct rdep, plus the sampled remainder"
    assert set(direct) <= set(closure.tested), "immediate consumers are never sampled out"
    assert closure.equivalence is Equivalence.CLOSURE_SAMPLED


def test_the_sample_is_reproducible_from_its_recorded_seed() -> None:
    """§3.4: "`rdeps_sample_seed` records the choice, so the sample is reproducible and
    auditable". `sha256`, not `hash()`: `hash()` of a `str` is salted per process, so a resume
    would re-sample a different set and quietly verify something else."""
    labels = [f"//pkg{i:05d}:lib" for i in range(3000)]
    first = select_tested_targets(labels, query="q", limit=100, sample_n=40)
    again = select_tested_targets(
        list(reversed(labels)), query="q", limit=100, sample_n=40, seed=first.seed
    )
    assert first.seed and again.tested == first.tested
    other = select_tested_targets(labels, query="q", limit=100, sample_n=40, seed="deadbeef")
    assert other.tested != first.tested, "a different seed must select a different sample"


def test_a_truncated_closure_forces_closure_sampled_on_the_report() -> None:
    """§3.4 + §3.5.1: `rdeps_truncated` can only ever pull FULL down to CLOSURE_SAMPLED, which
    forces the PR to a draft and renders a banner naming the count, the sample and the seed. The
    report derives it — a caller cannot record the reduced set and an unqualified verdict."""
    closure = select_tested_targets(
        [f"//p{i}:l" for i in range(30)], query="q", direct=["//p0:l"], limit=5, sample_n=3
    )
    report = VerificationReport(
        run_id=RUN_ID, repo_id="acme-widget", build_ok=True, test_ok=True,
        rdeps_query=closure.query, rdeps_target_count=closure.total_count,
        rdeps_tested=closure.tested_count, rdeps_ok=True, rdeps_truncated=closure.truncated,
        verdict="PASS",
    )
    assert closure.truncated
    assert report.equivalence is Equivalence.CLOSURE_SAMPLED
    assert report.equivalence is closure.equivalence


def test_query_output_is_parsed_deterministically() -> None:
    """`bazel query` promises no ordering, and the order reaches a persisted decision (which
    targets were sampled). An unsorted parse makes the seed reproducible and the sample not."""
    stdout = "//b:b\nINFO: Elapsed\n\n//a:a\n//b:b\n# comment\n  //c:c  \n"
    assert parse_target_labels(stdout) == ("//a:a", "//b:b", "//c:c")


async def test_rdeps_closure_builds_the_argv_and_skips_the_depth_query_when_it_can() -> None:
    """The injected `CommandRunner` is the seam (guardrail 3): the argv that WOULD have run is
    asserted with no `bazel` on PATH. The depth-1 query costs a second `bazel query` per repo per
    attempt, so it runs only when the closure actually needs sampling."""
    calls: list[tuple[str, ...]] = []

    def make_runner(count: int):
        async def runner(argv, *, cwd=None, env=None, deadline=None, timeout_s=None):
            calls.append(tuple(argv))
            body = "".join(f"//pkg{i}:lib\n" for i in range(count))
            return ProcResult(argv=tuple(argv), exit_code=0, stdout_tail=body, stderr_tail="",
                              duration_ms=1, timed_out=False)
        return runner

    small = await rdeps_closure("java/acme", runner=make_runner(3), limit=2000)
    assert len(calls) == 1, "no depth-1 query when the closure fits under the bound"
    assert calls[0][0:2] == ("bazel", "query")
    assert "--keep_going" in calls[0] and calls[0][-1] == rdeps_query("java/acme")
    assert not small.truncated and small.tested_count == 3

    calls.clear()
    big = await rdeps_closure("java/acme", runner=make_runner(9), limit=4, sample_n=2)
    assert len(calls) == 2 and calls[1][-1].endswith(", 1)")
    assert big.truncated and big.equivalence is Equivalence.CLOSURE_SAMPLED

    # BOTH queries carry `build.registry`, not just the first: a closure resolved against one
    # module registry and sampled from a depth-1 query resolved against another is two different
    # module graphs reported as one blast radius.
    calls.clear()
    await rdeps_closure(
        "java/acme", runner=make_runner(9), limit=4, sample_n=2, registry="https://m.invalid/bcr"
    )
    assert all("--registry=https://m.invalid/bcr" in call for call in calls), calls


async def test_a_failed_query_is_never_read_as_an_empty_closure() -> None:
    """Rule 11. An empty target list from a failed query is indistinguishable from a clean
    closure of zero, and one of those two is a green PR for an untested blast radius."""
    async def runner(argv, *, cwd=None, env=None, deadline=None, timeout_s=None):
        return ProcResult(argv=tuple(argv), exit_code=1, stdout_tail="", stderr_tail="ERROR: bad",
                          duration_ms=1, timed_out=False)

    with pytest.raises(BazelQueryError, match="exited 1"):
        await rdeps_closure("java/acme", runner=runner)


def test_a_truncated_stdout_with_no_log_file_is_refused() -> None:
    """§11.3 caps `stdout_tail` at 32 KiB; a 40 000-target closure is megabytes. Reading the tail
    would drop most of the blast radius and report the remainder as the whole of it."""
    result = ProcResult(argv=("bazel",), exit_code=0, stdout_tail="//a:a\n", stderr_tail="",
                        duration_ms=1, timed_out=False, stdout_bytes=10_000_000)
    with pytest.raises(BazelQueryError, match="kept no stdout file"):
        query_stdout("q", result)


def test_closure_defaults_match_the_bounds_table() -> None:
    """§3.4's defaults are 2 000 / 500. They live in code as named constants so a config change
    is visible against a documented baseline."""
    from fleet.bazel.query import DEFAULT_RDEPS_LIMIT, DEFAULT_SAMPLE_N

    assert (DEFAULT_RDEPS_LIMIT, DEFAULT_SAMPLE_N) == (2000, 500)
    assert RdepsClosure(query="q", total_count=0, tested=()).equivalence is Equivalence.FULL


# =======================================================================================
# MODULE.bazel.lock — the registry the lock is KEYED by vs. the registry the build CONTACTS
# =======================================================================================
#
# A controlled matrix run in the real `fleet-build:9.2.0-bookworm` image under real
# `docker run --network=none` established that both artifacts are required and neither
# substitutes for the other: warm repository cache + NO lockfile is exit 32 before analysis
# (`Error computing the main repository mapping … Unknown host: bcr.bazel.build`); warm cache +
# matching lockfile is exit 0; EMPTY cache + matching lockfile is exit 32 again. It also
# established the asymmetry these three tests are about — a cache warmed through the mirror
# serves a lock keyed by `bcr.bazel.build` (the cache is content-addressed, and both BCR
# addresses serve byte-identical files), so the mismatch that kills an offline build lives in the
# lockfile's URL keys and nowhere else.
#
# That makes the check a pure string comparison over published bytes, which is the entire point:
# it needs no container, no warm cache and no network, and it would have caught a mirror-keyed
# lock at the moment it was written instead of at the next offline build.


def _a_lockfile(registry: str, *, extension_urls: tuple[str, ...] = ()) -> str:
    """A minimal Bazel lockfile keyed by `registry`, in the shape Bazel 9 writes.

    Hand-built rather than captured from a real run on purpose: the subject of these tests is the
    URL keys, and a fixture whose keys a test can state is one whose failure names the defect. The
    `moduleExtensions` half carries DOWNLOAD urls as VALUES — archives the repository cache serves
    by SHA-256 — which is the distinction `lock_registry_urls` exists to make.
    """
    return json.dumps(
        {
            "lockFileVersion": 18,
            "registryFileHashes": {
                f"{registry}/bazel_registry.json": "8a2b" * 16,
                f"{registry}/modules/rules_go/0.61.1/MODULE.bazel": "1c3d" * 16,
                f"{registry}/modules/rules_go/0.61.1/source.json": "9e0f" * 16,
            },
            "selectedYankedVersions": {},
            "moduleExtensions": {
                "@@rules_go+//go:extensions.bzl%go_sdk": {
                    "general": {
                        "bzlTransitiveDigest": "ab" * 32,
                        "generatedRepoSpecs": {
                            "go_default_sdk": {
                                "attributes": {"urls": list(extension_urls)},
                            }
                        },
                    }
                }
            },
        },
        indent=2,
        sort_keys=True,
    )


def test_a_lockfile_keyed_by_the_registry_the_container_contacts_is_accepted() -> None:
    """The passing half, and it is not a formality: it is what stops the guard below from being
    a check that refuses everything.

    `BCR_DEFAULT_REGISTRY` is the expected value because `buildverify._bazel_argv` emits **no**
    `--registry` — the flag exists (`bazel/query.registry_args`, `build.registry`) but reaches
    only `query`/`test` argv construction, so the Bazel that runs the containerised build resolves
    against its own built-in default and nothing else. The harness also generates no `.bazelrc`
    (`layout.MODULE_FILES` names one; no generator writes one), so there is no second layer that
    could redirect it.
    """
    check_lock_registry(_a_lockfile(BCR_DEFAULT_REGISTRY), registry=BCR_DEFAULT_REGISTRY)


def test_a_mirror_keyed_lockfile_is_refused_and_the_message_states_the_offline_consequence(
) -> None:
    """The failure this guard exists for, and the reason its message is asserted rather than its
    type.

    A lock resolved through `BCR_MIRROR_REGISTRY` is *correct* — the mirror serves the same bytes
    — and it is also useless to a container that contacts `bcr.bazel.build`, because
    `registryFileHashes` is keyed by the whole URL: not one of its keys is ever looked up, so
    Bazel re-fetches the registry metadata it already has, offline, and dies before analysis. A
    reader who is told only "hosts differ" has no way to know that, and would reasonably conclude
    the lock is fine because the mirror is legitimate. So the message has to carry the
    consequence — the phase, the exit code, and the fact that the tree looks offline-ready.
    """
    mirror = _a_lockfile(BCR_MIRROR_REGISTRY)

    with pytest.raises(LockfileRegistryMismatchError) as caught:
        check_lock_registry(mirror, registry=BCR_DEFAULT_REGISTRY)

    message = str(caught.value)
    assert "raw.githubusercontent.com" in message, message
    assert BCR_DEFAULT_REGISTRY in message, message
    # The consequence, not merely the mismatch.
    assert "computing the main repository mapping" in message, message
    assert "exit 32" in message, message
    assert "--network=none" in message, message
    assert "LOOKS offline-ready" in message, message
    # And the offending key itself, so the reader can go find it.
    assert f"{BCR_MIRROR_REGISTRY}/bazel_registry.json" in message, message


def test_the_archive_urls_a_lockfile_holds_as_VALUES_are_not_the_subject() -> None:
    """The guard must not refuse a real lockfile, and a real one is full of foreign hosts.

    `moduleExtensions` records what each extension produced, including the `urls` of every
    `http_archive` it created — github.com, registry.npmjs.org, storage.googleapis.com. Those are
    **values**, fetched by SHA-256 through the repository cache, which is exactly why the cache is
    registry-agnostic; a check that treated them as registry keys would fail every lock the fleet
    will ever publish and would be deleted within a day.

    Asserted through `lock_registry_urls` as well as through the checker so the reason is visible:
    the keys are the three registry files and nothing else.
    """
    lock = _a_lockfile(
        BCR_DEFAULT_REGISTRY,
        extension_urls=(
            "https://github.com/bazelbuild/rules_go/releases/download/v0.61.1/x.zip",
            "https://storage.googleapis.com/golang/go1.24.linux-amd64.tar.gz",
        ),
    )

    assert lock_registry_urls(lock) == [
        f"{BCR_DEFAULT_REGISTRY}/bazel_registry.json",
        f"{BCR_DEFAULT_REGISTRY}/modules/rules_go/0.61.1/MODULE.bazel",
        f"{BCR_DEFAULT_REGISTRY}/modules/rules_go/0.61.1/source.json",
    ]
    check_lock_registry(lock, registry=BCR_DEFAULT_REGISTRY)


# =======================================================================================
# the tests that need the real toolchain (`tools/bin/bazel` — bazelisk → Bazel 9.2.0)
# =======================================================================================
#
# `bazel` IS installed here now, wired onto PATH by `tests/conftest.py`. What that buys and what
# it does not is spelled out in `docs/INTEGRATION_HONESTY.md`; the short version is that these
# are *acceptance* checks on the generator's output — Bazel's own parser and its own MVS reading
# the bytes we emit — and nothing here compiles a line of anyone's source.
#
# No host JDK is required: the Bazel release bazelisk fetches embeds its own JVM, which is why
# `java -version` failing on this host does not gate any of this.


_FETCH_FLAGS = (
    f"--repository_cache={BAZEL_REPOSITORY_CACHE}",
    f"--http_timeout_scaling={HTTP_TIMEOUT_SCALING}",
)
"""The same two fetch options `conftest.bazel_fetch_bazelrc` writes, as argv.

These tests drive Bazel directly, so they cannot pick up a workspace `.bazelrc` the way the
`fleet build` tests do — and for a long time they did not carry the equivalent flags either. The
consequence was not a style divergence: with no `--repository_cache`, every download landed in
the DEFAULT cache under `--output_user_root`, which `pytest_sessionfinish` reaps with the rest of
the output base. So this file re-fetched ~230 MB of ruleset and toolchain archives every session
— archives `tests/test_build_e2e.py` had already put in the kept cache — and paid a real network
timeout for them, which is how the first real-Bazel test of a session intermittently failed with
`Read timed out` on `node-v22.22.0-linux-x64.tar.xz` and then passed when run alone.

`HTTP_TIMEOUT_SCALING` (8.0) replaces the 5.0 this file used to ride in on registry-carrying
commands only. One number now, and it is the MEASURED one: conftest calibrated 8.0 against this
host's slow `objects.githubusercontent.com` redirect, where 5.0 was sized against a single
`MODULE.bazel` fetch racing a cold server start. Both flags go on every command rather than only
the registry-carrying ones, because a cache miss and a slow redirect do not care whether
`--registry=` was passed."""


def _bazel(
    startup: tuple[str, ...],
    *args: str,
    cwd: Path,
    registry: tuple[str, ...] = (),
    timeout_s: float = 600.0,
) -> subprocess.CompletedProcess[str]:
    """Real bazel, out of the workspace-local output base the session fixture pins.

    `registry` is the `bazel_registry_args` fixture — `--registry=<url>` built by
    `fleet.bazel.query.registry_args`, the same function the harness's own argv goes through. It
    is appended last: Bazel separates options from residue, so a query expression or a target
    pattern earlier in `args` is unaffected.

    `_FETCH_FLAGS` rides along on every command: the session-stable repository cache, so the
    ruleset and toolchain archives are fetched once per checkout instead of once per session, and
    the timeout headroom this host's slow GitHub redirect needs. Neither changes what Bazel
    analyses. A one-off network hiccup must not be reported as a generator defect — but since
    these no longer skip, the way to keep that honest is to stop re-fetching and to give what is
    left room, not to forgive the failure.
    """
    BAZEL_REPOSITORY_CACHE.mkdir(parents=True, exist_ok=True)
    return subprocess.run(  # noqa: S603
        [*startup, *args, *registry, *_FETCH_FLAGS],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_s,
    )


_REGISTRY_UNREACHABLE = (
    "Could not resolve",
    "Connection",
    "Error accessing registry",
    "Connect timed out",
    "Read timed out",
)
"""How a Bazel that could not reach its registry says so.

Enumerated rather than "any non-zero exit" because these tests assert *specific* failures: a
guard that fired on every error would turn the negative controls below into no-ops. The list is
longer than the `"Could not resolve"` / `"Connection"` pair it started as because that pair does
not match a registry that accepts the socket and then stalls — `Error accessing registry …:
Connect timed out`, which is what this host gets from `bcr.bazel.build`, and which was once
reported as five generator defects."""


def _fail_if_registry_unreachable(
    proc: subprocess.CompletedProcess[str], registry: tuple[str, ...]
) -> None:
    """A registry error HERE is a failure, not a skip.

    **Why this is not `pytest.skip` any more.** It was, and the consequence is the reason this
    guard was rewritten: every one of these tests skipped on this host, a skip is rendered as a
    non-failure in every summary line, and the only real check the suite has on generated
    `MODULE.bazel` output quietly stopped running while looking green. `conftest.bazel_registry`
    has already proved this registry answers a plain HTTPS GET before the test started, so
    reaching this point means the registry died mid-run or Bazel cannot use the one that was
    configured — both of which are findings, not weather. An operator who genuinely has no
    registry declares it with `FLEET_TEST_ALLOW_OFFLINE_BAZEL=1` and gets a skip whose reason
    names the URLs and the errors, at session scope, once.
    """
    if any(marker in proc.stderr for marker in _REGISTRY_UNREACHABLE):
        pytest.fail(
            f"the configured registry {registry or '(bazel default)'} was reachable at session "
            f"start and this command could not use it — a registry outage mid-run, not a "
            f"generator defect, but not a pass either:\n{proc.stderr[-1500:]}"
        )


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("bazel") is None, reason="bazel is not installed on this host")
def test_real_bazel_accepts_the_generated_module(
    bazel_workspace: Path, bazel_startup_argv: tuple[str, ...], bazel_registry_args: tuple[str, ...]
) -> None:
    """Real Bazel loads, analyses and BUILDS a package rendered entirely by the shipped
    generators — `bazel build //...`, not merely `query`.

    Why `build` and not `query`: `query` stops after loading, so a `BUILD.bazel` whose attributes
    are the wrong *type* for the rule (a string where a label list belongs, a `srcs` naming a file
    that is not there) still queries clean and only explodes in the analysis phase, on the
    monorepo, twenty waves in. `build` runs loading AND analysis, so those are caught here.

    This is deliberately a dependency-free module: it proves the emitted syntax and the emitted
    target shape, and it proves NOTHING about the ruleset wiring — that is the next test's job.
    """
    (bazel_workspace / "MODULE.bazel").write_text(
        render_module_bazel([], module_name="m", ruleset_versions={}), encoding="utf-8"
    )
    (bazel_workspace / "BUILD.bazel").write_text(
        render_build_bazel(
            [BuildTarget(package="", name="all_srcs", rule="filegroup", srcs=["MODULE.bazel"])]
        ),
        encoding="utf-8",
    )
    queried = _bazel(
        bazel_startup_argv,
        "query",
        "//...",
        cwd=bazel_workspace,
        registry=bazel_registry_args,
    )
    _fail_if_registry_unreachable(queried, bazel_registry_args)
    assert queried.returncode == 0, queried.stderr
    assert "//:all_srcs" in queried.stdout, queried.stdout

    built = _bazel(
        bazel_startup_argv, "build", "//...", cwd=bazel_workspace, registry=bazel_registry_args
    )
    _fail_if_registry_unreachable(built, bazel_registry_args)
    assert built.returncode == 0, built.stderr
    assert "Build completed successfully" in built.stderr, built.stderr


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("bazel") is None, reason="bazel is not installed on this host")
def test_real_bazel_resolves_the_generated_bazel_dep_lines(
    bazel_workspace: Path, bazel_startup_argv: tuple[str, ...], bazel_registry_args: tuple[str, ...]
) -> None:
    """Real MVS over a real registry, on the `bazel_dep` lines the shipped defaults produce.

    Why it matters: `render_module_bazel` renders `bazel_dep(name = …, version = …)` from
    `build.ruleset_versions` (§9), and a version string that the Bazel Central Registry does not
    carry is a `MODULE.bazel` that resolves on nobody's machine. Only a real resolution can say
    so — the offline tests above compare our own strings to our own strings.

    Network: this reaches BCR. It skips (accurately) when the registry is unreachable rather than
    reporting a network outage as a generator defect.
    """
    deps = PyAdapter().workspace_deps(
        BuildUnit(
            unit_id="acme-app",
            ecosystem=Ecosystem.PYPI,
            dest="py/acme-app",
            external_coordinates=[
                Coordinate(ecosystem=Ecosystem.PYPI, name="requests", version_spec=">=2")
            ],
        )
    )
    (bazel_workspace / "MODULE.bazel").write_text(
        render_module_bazel(
            deps,
            module_name="acme_monorepo",
            ruleset_versions=dict(BuildSection().ruleset_versions),
            toolchains=PyAdapter().toolchain_requirements(),
        ),
        encoding="utf-8",
    )
    (bazel_workspace / "BUILD.bazel").write_text("", encoding="utf-8")

    proc = _bazel(
        bazel_startup_argv, "mod", "graph", cwd=bazel_workspace, registry=bazel_registry_args
    )
    _fail_if_registry_unreachable(proc, bazel_registry_args)

    # The module name we emitted is the root, and the ruleset we emitted a `bazel_dep` for is in
    # the resolved graph — so the line parsed AND the registry has that module.
    assert "acme_monorepo@0.0.0" in proc.stdout, proc.stdout[-2000:]
    assert "rules_python@" in proc.stdout, proc.stdout[-2000:]
    # The transitive graph is real: rules_python pulls its own bazel_deps in. A registry that
    # answered with a stub, or a `bazel mod graph` that never left the machine, would not have.
    assert "bazel_skylib@" in proc.stdout, proc.stdout[-2000:]


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("bazel") is None, reason="bazel is not installed on this host")
def test_the_configured_ruleset_version_is_the_version_bazel_selects(
    bazel_workspace: Path, bazel_startup_argv: tuple[str, ...], bazel_registry_args: tuple[str, ...]
) -> None:
    """The version in `build.ruleset_versions` must be the version that ends up in the build.

    Why it matters: §9's whole reason for pinning is that two runs of the same fleet build the
    same bytes. A floor that a transitive dependency silently raises means the monorepo's Python
    rules change under the fleet whenever an unrelated module in BCR publishes — which is exactly
    the non-reproducibility the pin was introduced to prevent, and no offline test can see it
    because the raise happens inside Bazel's resolver.

    The assertion is on the version Bazel **selects**, read out of `bazel mod graph`, not on the
    text we emitted: this test passed against the emitted text for as long as the defect existed.
    `rules_python@1.0.0` reaches the resolver alongside modules that declare a floor of 1.7.0 —
    rules_python's own transitive deps do — so a selected 1.0.0 can only come from the
    `single_version_override` the generator now emits beside the `bazel_dep`.
    """
    pinned = BuildSection().ruleset_versions["rules_python"]
    (bazel_workspace / "MODULE.bazel").write_text(
        render_module_bazel(
            [],
            module_name="acme_monorepo",
            ruleset_versions={"rules_python": pinned},
            toolchains=[
                ToolchainRequirement(
                    ruleset="rules_python",
                    extension="python.toolchain",
                    name="python_3_12",
                    version="3.12",
                    attrs={"python_version": "3.12"},
                )
            ],
        ),
        encoding="utf-8",
    )
    (bazel_workspace / "BUILD.bazel").write_text("", encoding="utf-8")

    proc = _bazel(
        bazel_startup_argv, "mod", "graph", cwd=bazel_workspace, registry=bazel_registry_args
    )
    _fail_if_registry_unreachable(proc, bazel_registry_args)
    assert proc.returncode == 0, proc.stderr[-2000:]
    selected = set(re.findall(r"rules_python@([0-9][0-9A-Za-z.\-+]*)", proc.stdout))
    assert selected == {pinned}, (
        f"bazel SELECTED rules_python {sorted(selected)} for a configured {pinned}: the "
        f"bazel_dep floor was raised by a transitive module and the pin did not hold\n"
        f"{proc.stdout[-2000:]}"
    )


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("bazel") is None, reason="bazel is not installed on this host")
def test_real_bazel_evaluates_the_generated_module_extensions(
    bazel_workspace: Path, bazel_startup_argv: tuple[str, ...], bazel_registry_args: tuple[str, ...]
) -> None:
    """`use_extension` must name a `.bzl` the ruleset actually ships.

    Why it matters: `bazel_dep` resolving is not enough. `pip.parse` / `maven.install` are how
    every third-party dependency in the monorepo becomes a label, and an extension that fails to
    load means `@pypi//:requests` resolves to nothing — Phase 3 emits a MODULE.bazel that cannot
    build, for every repo in the fleet, with a message that points at the ruleset rather than at
    us. This is the assertion the offline generator tests structurally cannot make.

    `pip.parse` really runs here, so the lock file it names has to exist: an empty one is a hub
    with no wheels in it, which is what this test wants — the question is whether the extension
    LOADS and EVALUATES, not whether PyPI is up. A missing lock fails inside the extension
    (`FileNotFoundException` from `parse_requirements`), which would be a green-looking way to
    stop proving the thing this test exists for.
    """
    adapter = PyAdapter()
    deps = adapter.workspace_deps(
        BuildUnit(
            unit_id="acme-app",
            ecosystem=Ecosystem.PYPI,
            dest="py/acme-app",
            external_coordinates=[
                Coordinate(ecosystem=Ecosystem.PYPI, name="requests", version_spec=">=2")
            ],
        )
    )
    (bazel_workspace / "MODULE.bazel").write_text(
        render_module_bazel(
            deps,
            module_name="acme_monorepo",
            ruleset_versions=dict(BuildSection().ruleset_versions),
            toolchains=adapter.toolchain_requirements(),
        ),
        encoding="utf-8",
    )
    (bazel_workspace / "BUILD.bazel").write_text("", encoding="utf-8")
    (bazel_workspace / "requirements.lock").write_text("", encoding="utf-8")

    proc = _bazel(
        bazel_startup_argv, "mod", "graph", cwd=bazel_workspace, registry=bazel_registry_args
    )
    _fail_if_registry_unreachable(proc, bazel_registry_args)
    assert "extensions failed" not in proc.stderr, proc.stderr[-2000:]
    assert "no such file" not in proc.stderr, proc.stderr[-2000:]
    assert proc.returncode == 0, proc.stderr[-2000:]
    # Both of the adapter's extensions really were named, and both really loaded: `pip` from
    # `python/extensions:pip.bzl` and `python` from `python/extensions:python.bzl`. The old
    # guessed `@rules_python//:extensions.bzl` is in neither the file nor the resolver.
    text = (bazel_workspace / "MODULE.bazel").read_text(encoding="utf-8")
    assert 'pip = use_extension("@rules_python//python/extensions:pip.bzl", "pip")' in text, text
    assert '"@rules_python//python/extensions:python.bzl", "python")' in text, text
    assert "//:extensions.bzl" not in text, text


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("bazel") is None, reason="bazel is not installed on this host")
def test_real_bazel_builds_the_generated_python_package(
    bazel_workspace: Path, bazel_startup_argv: tuple[str, ...], bazel_registry_args: tuple[str, ...]
) -> None:
    """`bazel build` of a package whose `BUILD.bazel` came out of the shipped adapter + renderer,
    over a real tree — the two things that made Phase 3's output unbuildable, both asserted by
    rules_python rather than by us.

    Why it matters, and why `build` rather than `query`:

    * **Paths.** A `srcs` entry is resolved against the package that declares it, so a
      monorepo-root-relative `py/acme-svc/client.py` inside `//py/acme-svc` names
      `py/acme-svc/py/acme-svc/client.py`. `query` loads that BUILD file happily; only analysis
      says the file is not there. This unit is deliberately handed root-relative `srcs` — the
      shape a driver reading `git ls-files` from the monorepo root produces — so the assertion
      is that the *generator* re-roots them, not that the caller happened to.
    * **File kinds.** `pyproject.toml` in `py_library.srcs` is an ANALYSIS failure, verbatim:
      "is misplaced here (expected .py or .py3)". It is not dropped either: it moves to `data`,
      so the package keeps a file it reads at runtime.

    The `labels(srcs, …)` query is the second half: it reports what Bazel itself resolved each
    `srcs` entry to, so the assertion is on real labels inside the real package, not on the
    strings the generator wrote.
    """
    dest = "py/acme-svc"
    adapter = PyAdapter()
    unit = BuildUnit(
        unit_id="acme-svc",
        ecosystem=Ecosystem.PYPI,
        dest=dest,
        srcs=[
            f"{dest}/acme_svc/__init__.py",
            f"{dest}/acme_svc/client.py",
            f"{dest}/pyproject.toml",
        ],
        published=Coordinate(ecosystem=Ecosystem.PYPI, name="acme-svc"),
    )
    package = bazel_workspace / dest
    (package / "acme_svc").mkdir(parents=True)
    (package / "acme_svc" / "__init__.py").write_text("VERSION = '1'\n", encoding="utf-8")
    (package / "acme_svc" / "client.py").write_text("def get() -> int:\n    return 1\n", "utf-8")
    (package / "pyproject.toml").write_text('[project]\nname = "acme-svc"\n', encoding="utf-8")

    (bazel_workspace / "MODULE.bazel").write_text(
        render_module_bazel(
            [],
            module_name="acme_monorepo",
            ruleset_versions=dict(BuildSection().ruleset_versions),
            toolchains=adapter.toolchain_requirements(),
        ),
        encoding="utf-8",
    )
    (bazel_workspace / "BUILD.bazel").write_text("", encoding="utf-8")
    targets = adapter.generate_targets(unit)
    (package / "BUILD.bazel").write_text(render_build_bazel(targets), encoding="utf-8")

    built = _bazel(
        bazel_startup_argv,
        "build",
        f"//{dest}:all",
        cwd=bazel_workspace,
        registry=bazel_registry_args,
    )
    _fail_if_registry_unreachable(built, bazel_registry_args)
    assert built.returncode == 0, built.stderr[-3000:]
    assert "Build completed successfully" in built.stderr, built.stderr[-3000:]

    queried = _bazel(
        bazel_startup_argv,
        "query",
        f"labels(srcs, //{dest}:acme-svc)",
        cwd=bazel_workspace,
        registry=bazel_registry_args,
    )
    assert queried.returncode == 0, queried.stderr[-2000:]
    assert sorted(queried.stdout.split()) == [
        f"//{dest}:acme_svc/__init__.py",
        f"//{dest}:acme_svc/client.py",
    ], queried.stdout
    # Refused, not discarded: the file rules_python will not compile is still an input.
    data = _bazel(
        bazel_startup_argv,
        "query",
        f"labels(data, //{dest}:acme-svc)",
        cwd=bazel_workspace,
        registry=bazel_registry_args,
    )
    assert data.stdout.split() == [f"//{dest}:pyproject.toml"], data.stdout


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("bazel") is None, reason="bazel is not installed on this host")
def test_real_bazel_fails_an_empty_failing_stub_target_with_an_explicit_message(
    bazel_workspace: Path, bazel_startup_argv: tuple[str, ...], bazel_registry_args: tuple[str, ...]
) -> None:
    """§3.5 item 2 (task-68, Leg 2, `stub_failing_target`): `bazel build` of a never-published
    provider's `EMPTY_FAILING` stub package exits non-zero, with an explicit message naming the
    abandoned provider and its coordinate — never a green build that ships an empty package
    nothing consumes, and never a runtime failure a consumer discovers only later.

    A real build over a real `genrule`, exactly the shape
    `test_real_bazel_builds_the_generated_python_package` above uses for the success case: the
    target comes out of the shipped, generic (non-per-ecosystem) rendering function and is
    written to disk, and Bazel itself is what fails the action — not a string check on generated
    text.

    The target NAME is `cli._internal_label(...)`'s own leaf (fix round, review finding I2), not
    a hand-picked string: that is the exact label `cli._unit_deps`'s redirect and
    `cli._create_stub_records`'s `bazel_label` column both use, so this test builds at the SAME
    label a real redirected consumer would name.
    """
    coord_key = "npm::acme-abandoned-lib"
    provider_repo_id = "acme-abandoned-lib"
    dest = stub_dest(coord_key)
    label = cli._internal_label(dest)
    package, _, name = label.removeprefix("//").partition(":")
    target = stub_failing_target(
        name=name, coord_key=coord_key, provider_repo_id=provider_repo_id, dest=package
    )

    (bazel_workspace / "MODULE.bazel").write_text(
        render_module_bazel([], module_name="acme_monorepo", ruleset_versions={}),
        encoding="utf-8",
    )
    (bazel_workspace / "BUILD.bazel").write_text("", encoding="utf-8")
    package_dir = bazel_workspace / package
    package_dir.mkdir(parents=True)
    (package_dir / "BUILD.bazel").write_text(render_build_bazel([target]), encoding="utf-8")

    built = _bazel(
        bazel_startup_argv,
        "build",
        label,
        cwd=bazel_workspace,
        registry=bazel_registry_args,
    )
    _fail_if_registry_unreachable(built, bazel_registry_args)
    assert built.returncode != 0, built.stderr[-3000:]
    assert provider_repo_id in built.stderr, built.stderr[-3000:]
    assert coord_key in built.stderr, built.stderr[-3000:]
    # (Fix round, review finding M2: the PREVIOUS form of this test asserted the declared output
    # was absent from the SOURCE package — a path Bazel never writes a declared `outs` file into
    # regardless of outcome, so the assertion could not fail either way. Dropped rather than
    # replaced with another non-discriminating check: `returncode != 0` together with the two
    # message assertions above already establish the failure is OUR `cmd`'s exit code and message,
    # not a generic "output missing" error from Bazel itself — which is the actual property that
    # needed proving.)


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("bazel") is None, reason="bazel is not installed on this host")
def test_real_bazel_resolves_a_redirected_consumer_through_a_published_artifact_stub_alias(
    bazel_workspace: Path, bazel_startup_argv: tuple[str, ...], bazel_registry_args: tuple[str, ...]
) -> None:
    """§3.5 item 1's PACKAGE half (fix round, review finding I1): a consumer redirected to a
    `PUBLISHED_ARTIFACT` stub — via exactly the label `cli._unit_deps`'s already-landed redirect
    (Blocker C, round VI task 13) substitutes for the abandoned provider's own — must resolve
    under REAL Bazel, not merely appear as a string in generated text. Every stub test before
    this fix round, including this leg's own first landing, ran only under `FakeBazel`, which
    never asks Bazel to resolve anything — so a redirect that could never build passed every one
    of them.

    **Python, not Maven — a deliberate choice, not the first one tried.** A maven/guava attempt
    surfaced a SEPARATE, pre-existing, previously-undetected defect: `JvmAdapter.workspace_deps()`
    emits `maven.install(name=…, version=…)` per artifact (`bazel/generators.py`'s generic
    `_tag_attrs` fallback, since that adapter deliberately sets no `attrs`), but real
    `rules_jvm_external`'s `install` tag class has NO per-artifact `name`/`version` attrs at all —
    only `artifacts` (a `string_list` of `"group:artifact:version"` tuples) or the separate
    `maven.artifact(...)` tag class. Real Bazel: `Error: in 'install' tag, unknown attribute
    'version' provided`. No test anywhere in this tree had ever run a REAL `bazel build`/`mod
    graph` against a generated `maven.install` tag before this attempt — every existing maven
    assertion (including `test_module_bazel_carries_the_workspace_deps_the_adapters_declare`) is
    `FakeBazel`-seamed. This is OUT OF SCOPE for task-68 (a pre-existing `ecosystems/jvm.py`/
    `bazel/generators.py` defect, unrelated to the stub-render mechanism) and is disclosed to the
    controller in this round's report rather than fixed here — fixing it correctly means changing
    how EVERY maven-ecosystem repo's `MODULE.bazel` renders, a blast radius well beyond this leg.

    Real PyPI instead, reusing the version-free-by-dialect `pip.parse` shape this suite's other
    real-bazel Python tests already exercise (`test_real_bazel_builds_the_generated_python_package`
    et al.) — `six` 1.16.0, a tiny, pure, dependency-free, single-module wheel, resolved for real
    via a real `requirements.lock`. The stub package (`stub_alias_target`, produced the identical
    way `cli._stub_package_files` produces it in production) is a generic `alias` forwarding to
    `@pypi//six`; the "consumer" `py_library` depends on the stub's label via
    `PyAdapter.generate_targets`'s own `dep_labels()`/`InternalDep` path — the SAME shape
    `_unit_deps`'s redirect would hand a real consumer, never a hand-rolled `deps` list.
    """
    coord_key = "pypi::six"
    pinned_version = "1.16.0"
    dest = stub_dest(coord_key)
    label = cli._internal_label(dest)
    package, _, name = label.removeprefix("//").partition(":")

    adapter = PyAdapter()
    six = Coordinate(ecosystem=Ecosystem.PYPI, name="six", version_spec=pinned_version)
    stub_unit = BuildUnit(
        unit_id=name,
        ecosystem=Ecosystem.PYPI,
        dest=package,
        published=six,
        internal_deps=[],
        external_coordinates=[six],
    )
    deps = adapter.workspace_deps(stub_unit)
    labels = adapter.external_labels(stub_unit)
    assert labels == ["@pypi//six"], labels
    stub_target = stub_alias_target(name=name, dest=package, actual=labels[0])

    consumer_dest = "py/acme_consumer"
    consumer_unit = BuildUnit(
        unit_id="acme-consumer",
        ecosystem=Ecosystem.PYPI,
        dest=consumer_dest,
        srcs=[f"{consumer_dest}/acme_consumer/__init__.py"],
        published=Coordinate(ecosystem=Ecosystem.PYPI, name="acme-consumer"),
        internal_deps=[InternalDep(label=label, dest=package, published=six)],
    )
    consumer_targets = adapter.generate_targets(consumer_unit)

    (bazel_workspace / "MODULE.bazel").write_text(
        render_module_bazel(
            deps,
            module_name="acme_monorepo",
            ruleset_versions=dict(BuildSection().ruleset_versions),
            toolchains=adapter.toolchain_requirements(),
        ),
        encoding="utf-8",
    )
    (bazel_workspace / "BUILD.bazel").write_text("", encoding="utf-8")
    (bazel_workspace / "requirements.lock").write_text("six==1.16.0\n", encoding="utf-8")
    stub_pkg_dir = bazel_workspace / package
    stub_pkg_dir.mkdir(parents=True)
    (stub_pkg_dir / "BUILD.bazel").write_text(render_build_bazel([stub_target]), encoding="utf-8")

    consumer_pkg_dir = bazel_workspace / consumer_dest / "acme_consumer"
    consumer_pkg_dir.mkdir(parents=True)
    (consumer_pkg_dir / "__init__.py").write_text("VERSION = '0.1'\n", encoding="utf-8")
    (bazel_workspace / consumer_dest / "BUILD.bazel").write_text(
        render_build_bazel(consumer_targets), encoding="utf-8"
    )

    built = _bazel(
        bazel_startup_argv,
        "build",
        f"//{consumer_dest}/...",
        cwd=bazel_workspace,
        registry=bazel_registry_args,
    )
    _fail_if_registry_unreachable(built, bazel_registry_args)
    assert built.returncode == 0, built.stderr[-3000:]

    # The redirect label ITSELF resolves to the real external target, not to nothing: this is
    # the exact fact Blocker C's own `FakeBazel`-seam tests cannot observe.
    queried = _bazel(
        bazel_startup_argv,
        "query",
        f"deps({label})",
        cwd=bazel_workspace,
        registry=bazel_registry_args,
    )
    assert queried.returncode == 0, queried.stderr[-2000:]
    assert "@pypi//six:six" in queried.stdout.split(), queried.stdout


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("bazel") is None, reason="bazel is not installed on this host")
def test_real_bazel_resolves_a_load_whose_ruleset_only_a_target_names(
    bazel_workspace: Path, bazel_startup_argv: tuple[str, ...], bazel_registry_args: tuple[str, ...]
) -> None:
    """D6: a ruleset named ONLY by a target's `load_from` must still get a `bazel_dep`, and the
    proof is Bazel resolving the load — not our own text.

    Why the assertion cannot be `'bazel_dep(name = "rules_python")' in text`: that string says
    nothing about whether Bazel can then find `@rules_python//python:defs.bzl`. The ruleset set
    used to be `{d.ruleset for d in workspace_deps} | {t.ruleset for t in toolchains}`, so a
    package with no external coordinates and no toolchain — a library that merely loads a rule —
    contributed nothing, and real Bazel answered `Unable to find package for @@[unknown repo
    'rules_python' requested from @@]//python:defs.bzl`. The FIRST half of this test renders
    exactly that module (the same call, without `targets`) and asserts the real failure, so a
    regression cannot pass by making the second half vacuous.
    """
    dest = "py/acme"
    package = bazel_workspace / dest
    package.mkdir(parents=True)
    (package / "acme.py").write_text("VALUE = 1\n", encoding="utf-8")
    (bazel_workspace / "BUILD.bazel").write_text("", encoding="utf-8")
    targets = [
        BuildTarget(
            package=dest,
            name="acme",
            rule="py_library",
            load_from="@rules_python//python:defs.bzl",
            srcs=["acme.py"],
        )
    ]
    (package / "BUILD.bazel").write_text(render_build_bazel(targets), encoding="utf-8")
    pinned = BuildSection().ruleset_versions["rules_python"]

    def render(*, with_targets: bool) -> None:
        (bazel_workspace / "MODULE.bazel").write_text(
            render_module_bazel(
                [],
                module_name="acme_monorepo",
                ruleset_versions={"rules_python": pinned},
                targets=targets if with_targets else (),
            ),
            encoding="utf-8",
        )

    # The defect, reproduced against the real loader: nothing DECLARED rules_python.
    render(with_targets=False)
    without = _bazel(
        bazel_startup_argv,
        "build",
        f"//{dest}:all",
        cwd=bazel_workspace,
        registry=bazel_registry_args,
    )
    _fail_if_registry_unreachable(without, bazel_registry_args)
    assert without.returncode != 0, without.stdout
    assert "unknown repo 'rules_python'" in without.stderr, without.stderr[-2000:]

    render(with_targets=True)
    built = _bazel(
        bazel_startup_argv,
        "build",
        f"//{dest}:all",
        cwd=bazel_workspace,
        registry=bazel_registry_args,
    )
    _fail_if_registry_unreachable(built, bazel_registry_args)
    assert built.returncode == 0, built.stderr[-3000:]
    assert "Build completed successfully" in built.stderr, built.stderr[-3000:]


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("bazel") is None, reason="bazel is not installed on this host")
def test_real_bazel_builds_the_generated_jvm_package(
    bazel_workspace: Path, bazel_startup_argv: tuple[str, ...], bazel_registry_args: tuple[str, ...]
) -> None:
    """D121: a JVM package's ONLY connection to `rules_java` is a target's `load_from` — same
    shape as D6's `rules_python` case above, and the same real fix: `rules_java` must be pinned in
    `build.ruleset_versions` for `render_module_bazel`'s `loaded`-set admission to name it.

    Before this fix, `bazel build` on the SAME generated files failed at package-LOAD time —
    before any target-level analysis — with `Unable to find package for @@[unknown repo
    'rules_java' requested from @@]//java:defs.bzl` (docs/INTEGRATION_HONESTY.md D121). This test
    proves both halves, same as the `rules_python` test above: the failure with `targets=()`, and
    the fix with `targets` supplied.

    `--java_runtime_version=remotejdk_21` is test-invocation plumbing ONLY — it stands in for
    whatever JDK-toolchain story a future task decides (see D121's own "sizing/design note" on the
    JDK-toolchain gap `jvm.py::toolchain_requirements()` discloses); it is not something
    `render_module_bazel` or `toolchain_requirements()` emits, and this test must not be read as
    resolving that separate, still-open question.
    """
    dest = "java/com/acme/widgets"
    package = bazel_workspace / dest
    package.mkdir(parents=True)
    (package / "Widget.java").write_text(
        "package com.acme.widgets;\n"
        "public class Widget {\n"
        "    public static int value() { return 42; }\n"
        "}\n",
        encoding="utf-8",
    )
    (bazel_workspace / "BUILD.bazel").write_text("", encoding="utf-8")
    targets = [
        BuildTarget(
            package=dest,
            name="widgets",
            rule="java_library",
            load_from="@rules_java//java:defs.bzl",
            srcs=["Widget.java"],
        )
    ]
    (package / "BUILD.bazel").write_text(render_build_bazel(targets), encoding="utf-8")
    pinned = BuildSection().ruleset_versions["rules_java"]

    def render(*, with_targets: bool) -> None:
        (bazel_workspace / "MODULE.bazel").write_text(
            render_module_bazel(
                [],
                module_name="acme_monorepo",
                ruleset_versions={"rules_java": pinned},
                targets=targets if with_targets else (),
            ),
            encoding="utf-8",
        )

    # The defect, reproduced against the real loader: nothing DECLARED rules_java.
    render(with_targets=False)
    without = _bazel(
        bazel_startup_argv,
        "build",
        f"//{dest}:all",
        cwd=bazel_workspace,
        registry=bazel_registry_args,
    )
    _fail_if_registry_unreachable(without, bazel_registry_args)
    assert without.returncode != 0, without.stdout
    assert "unknown repo 'rules_java'" in without.stderr, without.stderr[-2000:]

    render(with_targets=True)
    built = _bazel(
        bazel_startup_argv,
        "build",
        f"//{dest}:all",
        "--java_runtime_version=remotejdk_21",
        cwd=bazel_workspace,
        registry=bazel_registry_args,
    )
    _fail_if_registry_unreachable(built, bazel_registry_args)
    assert built.returncode == 0, built.stderr[-3000:]
    assert "Build completed successfully" in built.stderr, built.stderr[-3000:]


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("bazel") is None, reason="bazel is not installed on this host")
def test_use_repo_brings_a_toolchain_extensions_repos_into_scope(
    bazel_workspace: Path, bazel_startup_argv: tuple[str, ...], bazel_registry_args: tuple[str, ...]
) -> None:
    """D11: a module extension's repos exist only for modules that `use_repo` them, and the
    toolchain half of `render_module_bazel` emitted none.

    Why it matters: `ext.deps(ts_version = …)` creates `@npm_typescript` and every `ts_project`
    resolves its compiler through it, so the missing import is `no such package '@@[unknown repo
    'npm_typescript' requested from @@]//'` for every TypeScript repo in the fleet — a message
    that names a repo nobody wrote and points at the ruleset instead of at us. `rules_python`
    stands in for `rules_ts` here only because `aspect_rules_ts@3.5.0` cannot load under Bazel
    9.2.0 at all (its `aspect_bazel_lib` reads `@local_config_platform`, removed in Bazel 9);
    the *construct* under test is `use_repo`, which is ruleset-independent.

    **This is also the evidence for `repo_names` being a LIST.** One `python.toolchain` tag call
    creates `python_3_12`, `python_3_12_host` and `pythons_hub`, and Bazel resolves all three
    below — a single `repo_name` field could import exactly one of them and would leave the
    others as `unknown repo` at the first label that touches them.
    """
    pinned = BuildSection().ruleset_versions["rules_python"]
    exported = ["python_3_12", "python_3_12_host", "pythons_hub"]
    (bazel_workspace / "BUILD.bazel").write_text("", encoding="utf-8")

    def render(*, repo_names: list[str]) -> None:
        (bazel_workspace / "MODULE.bazel").write_text(
            render_module_bazel(
                [],
                module_name="acme_monorepo",
                ruleset_versions={"rules_python": pinned},
                toolchains=[
                    ToolchainRequirement(
                        ruleset="rules_python",
                        extension="python.toolchain",
                        name="python_3_12",
                        version="3.12",
                        attrs={"python_version": "3.12"},
                        repo_names=repo_names,
                    )
                ],
            ),
            encoding="utf-8",
        )

    # The defect: the tag call is there, the repo it creates is not in scope.
    render(repo_names=[])
    without = _bazel(
        bazel_startup_argv,
        "mod",
        "show_repo",
        "@python_3_12",
        cwd=bazel_workspace,
        registry=bazel_registry_args,
    )
    _fail_if_registry_unreachable(without, bazel_registry_args)
    assert without.returncode != 0, without.stdout
    assert "No repo visible as python_3_12" in without.stderr, without.stderr[-2000:]

    render(repo_names=exported)
    for repo in exported:
        shown = _bazel(
            bazel_startup_argv,
            "mod",
            "show_repo",
            f"@{repo}",
            cwd=bazel_workspace,
            registry=bazel_registry_args,
        )
        _fail_if_registry_unreachable(shown, bazel_registry_args)
        assert shown.returncode == 0, f"@{repo}: {shown.stderr[-2000:]}"
        assert f"## @{repo}:" in shown.stdout, shown.stdout[-2000:]


# --------------------------------------------------------------------------------------
# D8 — every pinned ruleset version must LOAD under the Bazel that runs the pipeline
# --------------------------------------------------------------------------------------
# `ruleset -> (apparent repo name, a .bzl the ruleset ships, a symbol that .bzl exports)`.
# The apparent name is not always the module name: `rules_go` asks its dependents to call it
# `io_bazel_rules_go` (a `module(repo_name = …)` in ITS MODULE.bazel does not propagate — only the
# dependent's `bazel_dep(repo_name = …)` does), which is why the probe carries the name rather
# than deriving it from the module.
_RULESET_LOAD_PROBES: dict[str, tuple[str, str, str]] = {
    "aspect_rules_js": ("aspect_rules_js", "@aspect_rules_js//js:defs.bzl", "js_binary"),
    "aspect_rules_ts": ("aspect_rules_ts", "@aspect_rules_ts//ts:defs.bzl", "ts_project"),
    "gazelle": ("gazelle", "@gazelle//:def.bzl", "gazelle"),
    "rules_go": ("io_bazel_rules_go", "@io_bazel_rules_go//go:def.bzl", "go_library"),
    "rules_java": ("rules_java", "@rules_java//java:defs.bzl", "java_library"),
    "rules_jvm_external": ("rules_jvm_external", "@rules_jvm_external//:defs.bzl", "maven_install"),
    "rules_proto": ("rules_proto", "@rules_proto//proto:defs.bzl", "proto_library"),
    "rules_python": ("rules_python", "@rules_python//python:defs.bzl", "py_library"),
    "rules_rust": ("rules_rust", "@rules_rust//rust:defs.bzl", "rust_library"),
}


def test_every_pinned_ruleset_has_a_load_probe() -> None:
    """**Why:** the integration test below is parametrized over `_RULESET_LOAD_PROBES`, so a pin
    added to `build.ruleset_versions` with no probe would be silently unverified — the exact
    shape of D8, where the two rulesets anybody thought to check were checked and the other six
    were not. This runs offline so the omission is caught in the fast suite, not only on a host
    with a registry.
    """
    assert set(_RULESET_LOAD_PROBES) == set(BuildSection().ruleset_versions)


def _pinned_module_bazel(
    probes: Mapping[str, tuple[str, str, str]], pins: Mapping[str, str]
) -> str:
    """A root module that pins the WHOLE table, exactly as ADR-0041 pins it.

    Hand-written rather than `render_module_bazel`, on purpose: the renderer emits only the
    rulesets a unit's deps/toolchains/targets name, and the question here is not "does the
    renderer emit this" (three tests above cover that) but "is this VERSION loadable" — which has
    to be asked of every pin at once, because `single_version_override` makes the pins a single
    resolution that can conflict with each other.
    """
    lines = ['module(name = "probe", version = "0.0.0")']
    for ruleset in sorted(pins):
        repo = probes[ruleset][0]
        alias = f", repo_name = {repo!r}" if repo != ruleset else ""
        lines.append(f"bazel_dep(name = {ruleset!r}, version = {pins[ruleset]!r}{alias})")
    lines += [
        f"single_version_override(module_name = {r!r}, version = {pins[r]!r})" for r in sorted(pins)
    ]
    return "\n".join(lines).replace("'", '"') + "\n"


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("bazel") is None, reason="bazel is not installed on this host")
@pytest.mark.parametrize("ruleset", sorted(_RULESET_LOAD_PROBES))
def test_every_pinned_ruleset_version_loads_under_real_bazel(
    ruleset: str,
    bazel_workspace: Path,
    bazel_startup_argv: tuple[str, ...],
    bazel_registry_args: tuple[str, ...],
) -> None:
    """D8: each version in `build.ruleset_versions` must be one the installed Bazel can LOAD.

    Why this is a standing guard and not two fixes: ADR-0041 made the table a true pin
    (`single_version_override`), so the configured version is the version that runs and MVS can no
    longer float quietly past one a newer Bazel rejects. Under Bazel 9.2.0 the shipped pins gave
    `rules_rust@0.54.1` and `rules_go@0.50.1` "The CcInfo symbol has been removed"
    (`rustdoc_test.bzl`, `cross.bzl`), `gazelle@0.39.1` the same through rules_go, and
    `aspect_rules_ts@3.5.0` "No repository visible as '@local_config_platform' from repository
    '@@aspect_bazel_lib+'". Only the first and the last were reported; the other two were found by
    asking the whole table instead of the two known names. The failure is not partial — the
    `MODULE.bazel` does not load, so **every** repo in the fleet fails Phase 3 with a message that
    names the ruleset rather than this harness.

    Loading, not resolving: `bazel mod graph` (the test above) proves the registry carries the
    version, and every one of the four bad pins passed that. Only compiling the ruleset's own
    `.bzl` — which a `load()` in a BUILD file forces — reaches the removed symbol.

    Loading is necessary and NOT sufficient, which is why this is not the only D8 guard:
    `aspect_rules_js@2.1.3` passes this test and still cannot build anything, because its
    `rules_nodejs` floor breaks when a target is *configured*, one phase later.
    `test_real_bazel_analyses_the_generated_js_binary` is what reaches that.

    Each case pins the whole table, so a version that only fails *beside* its siblings (a
    `compatibility_level` bump, a floor a sibling's override contradicts) fails here too.
    """
    repo, bzl, symbol = _RULESET_LOAD_PROBES[ruleset]
    (bazel_workspace / "MODULE.bazel").write_text(
        _pinned_module_bazel(_RULESET_LOAD_PROBES, BuildSection().ruleset_versions),
        encoding="utf-8",
    )
    (bazel_workspace / "BUILD.bazel").write_text(f'load("{bzl}", "{symbol}")\n', encoding="utf-8")

    built = _bazel(
        bazel_startup_argv,
        "build",
        "//:all",
        cwd=bazel_workspace,
        registry=bazel_registry_args,
    )
    _fail_if_registry_unreachable(built, bazel_registry_args)
    assert built.returncode == 0, (
        f"build.ruleset_versions pins {ruleset}=={BuildSection().ruleset_versions[ruleset]}, "
        f"which this Bazel cannot load `{bzl}` from:\n{built.stderr[-3000:]}"
    )
    assert f"@{repo}" not in built.stderr, built.stderr[-2000:]


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("bazel") is None, reason="bazel is not installed on this host")
def test_real_bazel_analyses_the_generated_js_binary(
    bazel_workspace: Path, bazel_startup_argv: tuple[str, ...], bazel_registry_args: tuple[str, ...]
) -> None:
    """D7: the `js_binary` the JS adapter emits must be a target real Bazel accepts — and the
    dependency the removed `deps` was expressing must still be there.

    Why it has to be run rather than asserted on the emitted attribute list: the old target
    carried `deps = [":app"]`, which reads like every other rule in the file and is not one —
    rules_js's `js_binary` has no `deps` attribute, and Bazel says so only when the rule is
    *instantiated*: `//ts/acme/app:app_bin: no such attribute 'deps' in 'js_binary' rule`. An
    assertion over `BuildTarget.attrs` would have been green for the whole life of the defect,
    which is the entire D7 class of bug ("we emitted text that looks right").

    The negative control is the first half: the same generated package with `deps` spliced back
    in must fail, with that message. Without it, a regression could pass this test by emitting a
    `js_binary` that analyses for some unrelated reason.

    The edge is MOVED, not dropped, and the second assertion is on what Bazel resolved rather than
    on our string: `labels(data, //ts/acme/app:app_bin)` must still contain the `ts_project`. A
    `js_binary` whose runfiles lost the library analyses green and dies at `bazel run` with
    MODULE_NOT_FOUND — strictly worse than the loud failure it replaced.

    `--nobuild` stops after analysis: running `tsc` would prove the transpiler works, which is
    rules_ts's job, not this harness's, and would make the assertion depend on the npm registry.
    `tsconfig.json` is a real file this test writes because `ts_config` needs one on disk; the
    `ts_config` RULE is the adapter's own output now, so nothing is scaffolded into the package
    and what Bazel loads is exactly what Phase 3 would write.
    """
    adapter = JsAdapter()
    dest = "ts/acme/app"
    unit = BuildUnit(
        unit_id="acme-app",
        ecosystem=Ecosystem.NPM,
        dest=dest,
        srcs=[f"{dest}/src/index.ts"],
        published=Coordinate(ecosystem=Ecosystem.NPM, group="acme", name="app"),
    )
    targets = adapter.generate_targets(unit)
    package = bazel_workspace / dest
    (package / "src").mkdir(parents=True)
    (package / "src" / "index.ts").write_text("export const answer: number = 42;\n", "utf-8")
    (package / "tsconfig.json").write_text('{"compilerOptions": {"declaration": true}}\n', "utf-8")
    (bazel_workspace / "MODULE.bazel").write_text(
        render_module_bazel(
            [],
            module_name="acme_monorepo",
            ruleset_versions=dict(BuildSection().ruleset_versions),
            toolchains=adapter.toolchain_requirements(),
            targets=targets,
        ),
        encoding="utf-8",
    )
    (bazel_workspace / "BUILD.bazel").write_text("", encoding="utf-8")
    # No `ts_config` scaffold any more: the adapter emits the `:tsconfig` its `ts_project` names,
    # so writing one here is a duplicate rule and the package stops loading ("ts_config rule
    # 'tsconfig' conflicts with existing ts_config rule"). Asserted rather than assumed, because a
    # regression that dropped it again would otherwise turn this D7 test into a D10 test.
    generated = render_build_bazel(targets)
    assert 'ts_config(\n    name = "tsconfig"' in generated, generated
    assert "deps" not in generated, generated

    # Negative control: the defect, re-emitted verbatim into the same otherwise-good package. The
    # equality guard is not ceremony — a `replace` that silently matched nothing would make the
    # control assert that a CORRECT package fails, and the next person would delete the assertion.
    reintroduced = generated.replace(
        '    data = [\n        ":app",\n    ],', '    deps = [":app"],'
    )
    assert reintroduced != generated, generated
    (package / "BUILD.bazel").write_text(reintroduced, encoding="utf-8")
    broken = _bazel(
        bazel_startup_argv,
        "build",
        "--nobuild",
        f"//{dest}:all",
        cwd=bazel_workspace,
        registry=bazel_registry_args,
    )
    _fail_if_registry_unreachable(broken, bazel_registry_args)
    assert broken.returncode != 0, broken.stdout
    assert "no such attribute 'deps' in 'js_binary' rule" in broken.stderr, broken.stderr[-3000:]

    (package / "BUILD.bazel").write_text(generated, encoding="utf-8")
    analysed = _bazel(
        bazel_startup_argv,
        "build",
        "--nobuild",
        f"//{dest}:all",
        cwd=bazel_workspace,
        registry=bazel_registry_args,
    )
    _fail_if_registry_unreachable(analysed, bazel_registry_args)
    assert analysed.returncode == 0, analysed.stderr[-3000:]

    # The edge the `deps` was expressing, as Bazel resolved it — not as we spelled it.
    data = _bazel(
        bazel_startup_argv,
        "query",
        f"labels(data, //{dest}:app_bin)",
        cwd=bazel_workspace,
        registry=bazel_registry_args,
    )
    assert data.returncode == 0, data.stderr[-2000:]
    assert f"//{dest}:app" in data.stdout.split(), data.stdout


# ---------------------------------------------------------------------------------------
# the reaper itself — the mechanism the session finisher depends on
# ---------------------------------------------------------------------------------------
def test_the_bazel_reaper_removes_a_read_only_external_tree(tmp_path: Path) -> None:
    """`reap_bazel_state` really deletes a Bazel-shaped tree, read-only directories and all.

    **Why this is a test and not a comment.** Bazel strips the write bit from every fetched
    external repository directory, and `unlink` needs write permission on the *parent* — so the
    obvious `shutil.rmtree(..., ignore_errors=True)` walks the whole tree, fails on every entry,
    swallows each error and returns having deleted nothing. That is exactly how a cleanup path
    becomes a cleanup-shaped comment, and it is the fifth "mechanism that exists but never runs"
    defect this project has found; the incident that prompted this file's rewrite was measured at
    16 GB of output bases and 11 GB under `/tmp/pytest-of-<user>`.

    So the assertion is on the filesystem after the call, on a tree whose permissions reproduce
    the failure: without the chmod-and-retry handler this test fails, and `rm -rf` on the real
    output base fails the same way (it did, by hand, during this work).
    """
    output_base = tmp_path / "9f8e7d" / "external" / "rules_python++python"
    output_base.mkdir(parents=True)
    (output_base / "BUILD.bazel").write_text("filegroup(name = 'x')\n", encoding="utf-8")
    (output_base / "lib").mkdir()
    (output_base / "lib" / "tcltest.tm").write_text("package require Tcl\n", encoding="utf-8")
    for directory in (output_base / "lib", output_base):
        directory.chmod(0o555)  # r-x: what Bazel leaves behind

    residual = reap_bazel_state(tmp_path / "9f8e7d")

    assert residual == 0, "the reaper reported success while leaving bytes behind"
    assert not (tmp_path / "9f8e7d").exists()


def test_tree_bytes_counts_the_volume_not_the_symlink_farm(tmp_path: Path) -> None:
    """The peak figure the session reports must be the number the volume actually gave up.

    Why: a Bazel execroot is a forest of symlinks into `external/`, and a walker that followed
    them would report a multiple of the real usage — a "bound" inflated past the point of meaning
    anything, which fails the session for the wrong reason and teaches everyone to raise the
    ceiling. Hard links are counted once for the same reason.
    """
    real = tmp_path / "external" / "pkg"
    real.mkdir(parents=True)
    (real / "payload").write_bytes(b"x" * 4096)
    execroot = tmp_path / "execroot"
    execroot.mkdir()
    (execroot / "pkg").symlink_to(real, target_is_directory=True)
    (execroot / "hardlink").hardlink_to(real / "payload")

    assert tree_bytes(tmp_path) == 4096
