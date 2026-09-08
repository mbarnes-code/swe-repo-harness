"""Phases 3 and 4 end to end, through the real CLI, over real git repositories (§3.3, §3.4).

`tests/test_transform_e2e.py` proved Phase 2's composition; this file continues the same five
temporary repositories through `fleet build` and `fleet verify`, and it is the first test that
drives `scan → sequence → transform → build → verify` as one chain of real state transitions.

**Read this before believing any assertion below.** `bazel` and `git-filter-repo` ARE installed
on this host now (`tools/bin/bazel` → bazelisk → Bazel 9.2.0; `.venv/bin/git-filter-repo`; PATH
wired in `tests/conftest.py`), and section 7 at the bottom runs both for real. Doing so found
thirteen live defects — a double relocation of the ingested tree, generated BUILD files real
Bazel refused to analyse, lockfiles nobody resolved, a monorepo in which one TypeScript package
could not import another, and finally a fleet-wide root lockfile that kept one JS repo's
resolution and silently dropped the next repo's — every one of which is now closed and guarded by
a real build rather than pinned by a marker. Everything ABOVE section 7
still runs through the fakes, on purpose: the fakes are what make the state machine, the argv
and the failure classification assertable in two seconds instead of two minutes. What that
division buys and what it hides:

* *Proven by real execution.* Every git operation: the throwaway clone, the
  `git merge --allow-unrelated-histories` into `integration` with its ADR-0011 `Source-Repo:` /
  `Source-Sha:` trailers, the `IntegrationMutex` that serializes it, the immutable
  `refs/fleet/<run_id>/integration/<seq>` snapshot, the worktree cut from that ref, the commit
  and merge that publish the generated files. Every file: `BUILD.bazel` and `MODULE.bazel` are
  rendered by the shipped generators and asserted **on disk**. Every state transition: the wave
  gate, the phase rows, the `attempts` rows with their argv, exit codes, truncated stderr and
  `integration_ref`, and the exit codes the verb returns.
* *Proven only against an injected runner, in tests 1-6.* The `git-filter-repo` history rewrite
  and every `bazel` invocation. What is real about them is the **argv, the exit-code handling,
  the output parsing, the failure classification and everything downstream**; what is not real
  is that a build system ever looked at the tree, or that a single path moved. `FakeFilterRepo`
  in particular is a no-op on the tree, which is precisely why a doubled `<dest>/<dest>/` prefix
  survived every one of these tests. `cli.BAZEL_RUNNER` / `cli.FILTER_REPO_RUNNER` are the
  seams and both are `None` in production.
* *Proven by real execution of the real binaries, in section 7.* One real `fleet build` end to
  end, and real ingests whose rewritten history is walked commit by commit. The ingest tests
  assert the **exact path set** the merge brought in, per repo — never a `<dest>/` prefix, which
  a doubled `<dest>/<dest>/` path also satisfies and which is precisely how defect D3 survived.
  Two real `fleet build`s end to end, and real ingests whose rewritten history is walked commit
  by commit. The second is
  `test_a_dependencys_generated_package_is_on_the_branch_before_its_dependents_snapshot`, which
  passes: it pins the wave order, the publish-before-snapshot ordering read off the persisted
  `attempts.integration_ref`, and real Bazel analysing the DEPENDENCY's package out of the
  DEPENDENT's own build worktree. The first is `test_build_against_a_real_bazel`, which asserts
  exit 0 over all four fixture repos and carries no `xfail`. **No `xfail` exists in this file at
  all any more.** The last one was on
  `test_two_js_repos_with_different_npm_dependencies_both_build`, which adds a FIFTH repo — a
  second JS repo whose external npm package is not the first's — and pinned, `strict=True`, the
  defect that the fleet's one root `pnpm-lock.yaml` kept whichever JS repo sorted first and
  dropped the other's dependency while `fleet build` exited 0. ADR-0048 closed it (one pnpm
  importer per JS repo) and the marker was deleted rather than left passing, so that test is now
  a guard over real Bazel like every other one here.

The seams are deliberately narrow: `BAZEL_RUNNER` never reaches `Git`, so no git command in
Phase 3 is faked, and a merge or a snapshot this file asserts on really happened.

Each test says why it matters. Together:

* `scan → sequence → transform → build` reaches Phase 3 with real state transitions, and the
  generated `BUILD.bazel`/`MODULE.bazel` exist in the worktree with the content the plan implies;
* the build's argv is exactly §3.3's, and the tree it ran against is the immutable snapshot ref
  recorded on the `attempts` row — not the moving branch;
* the ADR-0010 sandbox really wraps the command in a `--network=none` container;
* a build failure is a structured `WorkerError` — exit code, TRUNCATED (never rejected) stderr,
  a real log file — and it costs exactly its own repo;
* Phase 4 does not start for a repo whose Phase 3 did not succeed;
* a truncated rdeps closure forces `CLOSURE_SAMPLED` and the report discloses the reduction,
  while an under-bound closure is verified whole;
* re-running `fleet build` duplicates no merge, no commit and no file;
* **the ecosystem adapters really decide the emission** (§6 below): a Maven module gets
  `java_library` and an npm package `ts_project`, with their `load()`s and their `@maven`/`@npm`
  labels; destinations come from `adapter.monorepo_dir`/`path_tail()` with no `dest:` written
  anywhere; `MODULE.bazel` carries the rulesets and MVS-resolved artifacts the adapters declared;
  a repo whose language nothing recognizes still gets the UNKNOWN adapter's `filegroup` and is
  still disclosed as `EcosystemAdapterUnavailable`; and `build.monorepo_dir_overrides` really
  moves a destination instead of being read and discarded.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from fleet import cli, ecosystems, manifests
from fleet.bazel.generators import (
    render_gazelle_build,
    render_module_bazel,
    render_root_package,
)
from fleet.bazel.layout import stub_dest
from fleet.bazel.lockfile import MODULE_LOCK_PATH, check_lock_registry
from fleet.cli import ExitCode, app
from fleet.ecosystems import go as go_adapter
from fleet.models.build import BuildUnit, SupportFile
from fleet.models.enums import Ecosystem
from fleet.models.repo import Coordinate
from fleet.models.state import MigrationState
from fleet.settings import (
    BCR_DEFAULT_REGISTRY,
    BCR_MIRROR_REGISTRY,
    BuildSection,
    FleetSettings,
    VerifySection,
)
from fleet.util.proc import ProcResult
from fleet.workers.buildgen import materialize
from fleet.workers.buildverify import C_TOOLCHAIN_PROBE
from tests.conftest import (
    BAZEL_REPOSITORY_CACHE,
    NO_C_COMPILER_OUTPUT_USER_ROOT,
    reap_bazel_state,
)
from tests.test_bazel import _a_lockfile, _fail_if_registry_unreachable
from tests.test_scan_e2e import FIXTURE_REPOS, _fresh_db, _make_repo
from tests.test_transform_e2e import (  # noqa: F401  (`fleet` is a fixture, used by injection)
    DESTINATIONS,
    TS_IMPORT_RULE,
    _write_config,
    _write_engine,
    base_args,
    fleet,
    git,
    query,
    scanned,
    transform,
    write_rules,
)

runner = CliRunner()

#: Every repo's rdeps closure, as `bazel query` would print it. `ts/acme/lib` is the one over the
#: bound this file drives Phase 4 with, so it is the one that must come back `CLOSURE_SAMPLED`.
CLOSURES: dict[str, tuple[str, ...]] = {
    "ts/acme/lib": (
        "//ts/acme/lib:lib",
        "//ts/acme/app:app",
        "//py/acme_lib_py:acme_lib_py",
        "//py/acme_app_py:acme_app_py",
        "//tools:fmt",
        "//third_party:vendored",
    ),
    "ts/acme/app": ("//ts/acme/app:app", "//tools:fmt"),
    "py/acme_lib_py": ("//py/acme_lib_py:acme_lib_py", "//py/acme_app_py:acme_app_py"),
    "py/acme_app_py": ("//py/acme_app_py:acme_app_py",),
}

#: Bigger than `LOG_TAIL_BYTES` on purpose: §7.1 says `stderr_tail` is TRUNCATED, never rejected,
#: and a 400 KB Gradle stderr that fails validation is an attempt that is never persisted.
LOUD_STDERR = ("ERROR: /work/ts/acme/app/BUILD.bazel:3:10: no such target\n" * 900) + "FAIL\n"

_DEST_IN_PATTERN = re.compile(r"//([^ )\"]+)/\.\.\.")


# ---------------------------------------------------------------------------------------
# the two injected runners — the ONLY two things in this file that are not really executed
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Invocation:
    """One command the harness would have executed, as the seam saw it."""

    argv: tuple[str, ...]
    cwd: Path | None
    #: The environment the driver handed the runner, exactly as the seam received it. Recorded
    #: because `Resolution.env` is an input to the bytes the resolver writes just as much as the
    #: argv is — `go mod download all` under `GOTOOLCHAIN=auto` and under `GOTOOLCHAIN=go1.24.12`
    #: are two different resolutions of one command — and a forward that silently stopped
    #: happening would leave every existing assertion here green.
    env: Mapping[str, str] | None = None

    @property
    def bazel(self) -> tuple[str, ...]:
        """The `bazel` command, unwrapped from the ADR-0010 container when there is one."""
        return self.argv[self.argv.index("bazel") :]

    @property
    def containerised(self) -> bool:
        return self.argv[0] == "docker"


class FakeFilterRepo:
    """`git-filter-repo`, recorded and not run.

    A no-op is the *honest* stand-in here and not a convenience: Phase 2 already relocated the
    tree by committing renames, so the branch tip this clone carries is byte-for-byte the tree
    the history rewrite would have produced at its tip. What stays unproven is the rewrite of the
    commits *behind* that tip — which is exactly what the skipped test at the bottom would prove.
    """

    def __init__(self) -> None:
        self.calls: list[Invocation] = []

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        deadline: float | None = None,
        timeout_s: float | None = None,
    ) -> ProcResult:
        _ = (env, deadline, timeout_s)
        self.calls.append(Invocation(tuple(argv), cwd))
        return ProcResult(
            argv=tuple(argv),
            exit_code=0,
            stdout_tail="",
            stderr_tail="",
            duration_ms=1,
            timed_out=False,
            cwd=cwd,
        )


#: What `FakeResolver` writes for each declared resolver, keyed by the tool the adapter names.
#: Deliberately *shaped like a real resolution* and not like the declared specs — a transitive
#: distribution the fleet never mentions (`certifi`), an `==` pin, a `packages:` section — because
#: the whole defect this seam exists for was a lock that carried none of those. It is still a
#: fake: what it proves is the plumbing (the argv, the inputs, the precedence, the file that lands
#: in the tree), and `test_the_resolved_lock_carries_the_transitive_closure` is where a REAL
#: resolver has to produce them.
FAKE_LOCKS: dict[str, tuple[str, str]] = {
    "uv": (
        "requirements.lock",
        "certifi==2024.7.4\n    # via requests\nrequests==2.32.3\n    # via -r requirements.in\n",
    ),
    "pnpm": (
        "pnpm-lock.yaml",
        "lockfileVersion: '9.0'\n\n"
        "importers:\n\n  .:\n    dependencies:\n      left-pad:\n"
        "        specifier: ^1.3.0\n        version: 1.3.0\n\n"
        "packages:\n\n  left-pad@1.3.0:\n    resolution: {integrity: sha512-fake}\n",
    ),
    #: `go mod download all`, and every part of this shape was measured against real Go rather
    #: than composed (ADR-0050). Both hash KINDS are here — the `h1:` module-zip hash and the
    #: `/go.mod` hash — because bare `go mod download` writes only the second, which is non-empty
    #: enough to pass the driver's empty-lock guard while `sums_from_go_mod`, reading for the
    #: `h1:`, still cannot use it. `go-spew` is the `certifi` of this entry: the `go.mod` never
    #: names it, so it can only be here because a resolver walked the transitive closure.
    "go": (
        "go.sum",
        "github.com/davecgh/go-spew v1.1.1 h1:vj9j/u1bqnvCEfJOwUhtlOARqs3+rkHYY13jYWTU97c=\n"
        "github.com/davecgh/go-spew v1.1.1/go.mod "
        "h1:J7Y8YcW2NihsgmVo/mv3lAwl/skON4iLHjSsI+c5H38=\n"
        "github.com/stretchr/testify v1.9.0 h1:HtqpIVDClZ4nwg75+f6Lvsy/wHu+3BoSGCbBAcpTsTg=\n"
        "github.com/stretchr/testify v1.9.0/go.mod "
        "h1:r2ic/lqez/lEtzL7wO/rwa5dbSLXVDPFyf8C91i36aY=\n",
    ),
}


def _scratch_contents(directory: Path) -> dict[str, str]:
    """Every file the driver wrote into a resolver's scratch directory, by relative POSIX path.

    Keyed by path and not by name, and walked recursively, because a `Resolution.input` may sit
    in a subdirectory — a first-party `link:ts/acme/lib` entry is resolved by pnpm *reading the
    manifest at that directory*, so `package.json` appears twice in one scratch and a name-keyed
    dict would silently keep whichever came last.
    """
    return {
        path.relative_to(directory).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


class FakeResolver:
    """`uv`/`pnpm`, recorded and answered from `FAKE_LOCKS`.

    The seam that keeps this file offline. A resolution is a network operation by nature — `uv`
    asks PyPI what `requests>=2.31` resolves to and `pnpm` asks the npm registry — so every test
    above section 7 injects this, and the two real-Bazel tests leave `cli.RESOLVER_RUNNER` `None`
    and pay for the real thing.

    It writes into the cwd it is handed, which is the harness's scratch directory, and it records
    the inputs that were written there first — so a test can assert that the driver really
    materialized `requirements.in` / `package.json` before running the resolver, rather than
    asserting on the argv alone and never noticing that the resolver had nothing to read.
    """

    def __init__(self) -> None:
        self.calls: list[Invocation] = []
        self.inputs: list[dict[str, str]] = []

    def tools(self) -> list[str]:
        return [call.argv[0] for call in self.calls]

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        deadline: float | None = None,
        timeout_s: float | None = None,
    ) -> ProcResult:
        _ = (deadline, timeout_s)
        self.calls.append(Invocation(tuple(argv), cwd, None if env is None else dict(env)))
        assert cwd is not None, argv
        name, content = FAKE_LOCKS[argv[0]]
        self.inputs.append(await asyncio.to_thread(_scratch_contents, cwd))
        await asyncio.to_thread((cwd / name).write_text, content, encoding="utf-8")
        return ProcResult(
            argv=tuple(argv),
            exit_code=0,
            stdout_tail="",
            stderr_tail="",
            duration_ms=3,
            timed_out=False,
            cwd=cwd,
        )


#: What `FakeGazelle` writes into the scratch tree, keyed by the `dest` the generator was pointed
#: at and then by the path RELATIVE to that dest.
#:
#: **Every entry is shaped like the vendored generator's real output over these exact fixtures,
#: and the SHAPE is the part that carries the test's meaning.** Three properties are modelled
#: because a capture that got any of them wrong would look like it worked:
#:
#: * `acme-clitool-go` is the corpus's binary layout — `cmd/<name>/main.go` plus an `internal/`
#:   package it imports — so its BUILD files are CREATED at depth 2 and 3 *below* its `dest`, and
#:   `go/clitool/BUILD.bazel` itself is left **byte-identical** (there is no `.go` file there).
#:   A capture of `<dest>/BUILD.bazel` alone would therefore capture nothing for this repo.
#: * `acme-digest-go` is the flat layout, so its `dest`-level file is **modified** — the text
#:   below is APPENDED to the directives the harness planted, which is why the `# gazelle:prefix`
#:   line has to survive into the published bytes. The real binary *rewrites* that file instead,
#:   putting its `load()` at the TOP and keeping the directives below it, so the two differ in
#:   statement ORDER and in nothing else; section 8 runs the binary and compares its labels to
#:   the ones below, file by file.
#: * The labels are the generator's own dialect: `@com_github_spf13_cobra//:go_default_library`
#:   and `@org_golang_x_crypto//blake2b:go_default_library` for `go_deps`-created repos, a bare
#:   package label `//go/clitool/internal/command` for an in-tree dependency, and
#:   `visibility = ["//go/clitool:__subpackages__"]` on `internal/`.
#:
#: It is still a FAKE. What it proves is the harness's own plumbing — the scratch that was
#: assembled, the argv it was invoked with, which files were captured back, and the bytes that
#: reached the branch. That no real Gazelle produced them, and that no Go was compiled, is stated
#: in every test below that uses it.
FAKE_GAZELLE_OUTPUT: dict[str, dict[str, str]] = {
    "go/clitool": {
        "cmd/clitool/BUILD.bazel": (
            '\nload("@io_bazel_rules_go//go:def.bzl", "go_binary", "go_library")\n'
            "\n"
            "go_library(\n"
            '    name = "clitool_lib",\n'
            '    srcs = ["main.go"],\n'
            '    importpath = "github.com/acme/clitool/cmd/clitool",\n'
            '    visibility = ["//visibility:private"],\n'
            '    deps = ["//go/clitool/internal/command"],\n'
            ")\n"
            "\n"
            "go_binary(\n"
            '    name = "clitool",\n'
            '    embed = [":clitool_lib"],\n'
            '    visibility = ["//visibility:public"],\n'
            ")\n"
        ),
        "internal/command/BUILD.bazel": (
            '\nload("@io_bazel_rules_go//go:def.bzl", "go_library")\n'
            "\n"
            "go_library(\n"
            '    name = "command",\n'
            '    srcs = ["root.go"],\n'
            '    importpath = "github.com/acme/clitool/internal/command",\n'
            '    visibility = ["//go/clitool:__subpackages__"],\n'
            '    deps = ["@com_github_spf13_cobra//:go_default_library"],\n'
            ")\n"
        ),
    },
    "go/digest": {
        "BUILD.bazel": (
            '\nload("@io_bazel_rules_go//go:def.bzl", "go_library")\n'
            "\n"
            "go_library(\n"
            '    name = "digest",\n'
            '    srcs = ["digest.go"],\n'
            '    importpath = "github.com/acme/digest",\n'
            '    visibility = ["//visibility:public"],\n'
            '    deps = ["@org_golang_x_crypto//blake2b:go_default_library"],\n'
            ")\n"
        )
    },
}


class FakeGazelle:
    """The BUILD-file generator, recorded and answered from `FAKE_GAZELLE_OUTPUT`.

    The seam that keeps the delegating half of §3.3 step 2 offline and reproducible. The real
    binary is a vendored Go program that walks a tree, resolves every `import` against a module
    graph and writes files; running it inside this suite would make these tests depend on a Go
    toolchain, a warm module cache and — in its default resolution mode — the network. What is
    under test here is the harness's own code on both sides of it: the scratch tree the driver
    assembles, the one argv it builds, and the capture that turns what came back into planned
    bytes.

    It writes into the roots it was handed and nowhere else, APPENDING to a file that already
    exists so that a directives file the harness planted is *modified* rather than replaced,
    which is what makes the "which bytes win" question real. The real generator rewrites such a
    file rather than appending to it — same labels, same surviving directives, different
    statement order — and section 8 is where that is measured instead of assumed.

    `trees` records the scratch exactly as the generator found it, so a test can assert that the
    driver really assembled the sources, the directives and the root `go.mod` before invoking it,
    rather than asserting on the argv alone and never noticing the generator had nothing to read.
    """

    def __init__(self) -> None:
        self.calls: list[Invocation] = []
        self.trees: list[dict[str, str]] = []

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        deadline: float | None = None,
        timeout_s: float | None = None,
    ) -> ProcResult:
        _ = (deadline, timeout_s)
        self.calls.append(Invocation(tuple(argv), cwd, None if env is None else dict(env)))
        assert cwd is not None, argv
        self.trees.append(await asyncio.to_thread(_scratch_contents, cwd))
        for arg in argv[1:]:
            if arg.startswith("-"):
                continue
            root = Path(arg)
            dest = root.relative_to(cwd).as_posix()
            for relative, text in sorted(FAKE_GAZELLE_OUTPUT.get(dest, {}).items()):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                existing = target.read_text(encoding="utf-8") if target.is_file() else ""
                target.write_text(existing + text, encoding="utf-8")
        return ProcResult(
            argv=tuple(argv),
            exit_code=0,
            stdout_tail="",
            stderr_tail="",
            duration_ms=5,
            timed_out=False,
            cwd=cwd,
        )


class FakeBazel:
    """`bazel`, recorded and answered from a table.

    Everything the harness does with the answer is the shipped code: `parse_target_labels`,
    `select_tested_targets`, `classify_build_failure`, `error_from_proc`, the `attempts` rows and
    the `VerificationReport`. The full stdout/stderr really reach disk, because
    `bazel/query.query_stdout` refuses to read a 40 000-target closure out of a 32 KiB tail and
    an in-memory fake would have hidden that requirement.
    """

    def __init__(
        self,
        log_root: Path,
        *,
        closures: Mapping[str, Sequence[str]] | None = None,
        fail: Mapping[tuple[str, str], int] | None = None,
    ) -> None:
        self.log_root = log_root
        self.closures = dict(closures or CLOSURES)
        self.fail = dict(fail or {})
        self.calls: list[Invocation] = []
        self.probes: list[Invocation] = []
        """The non-bazel commands the seam saw — today, only the sandboxed C-compiler probe."""
        self._seq = 0

    # -- the recorded views a test asserts on ------------------------------------------

    def commands(self, sub: str) -> list[tuple[str, ...]]:
        return [c.bazel for c in self.calls if c.bazel[1] == sub]

    def for_dest(self, sub: str, dest: str) -> list[Invocation]:
        return [
            c
            for c in self.calls
            if c.bazel[1] == sub and any(f"//{dest}/..." == arg for arg in c.bazel)
        ]

    # -- the seam ----------------------------------------------------------------------

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        deadline: float | None = None,
        timeout_s: float | None = None,
    ) -> ProcResult:
        _ = (env, deadline, timeout_s)
        call = Invocation(tuple(argv), cwd)
        if "bazel" not in call.argv:
            # `BuildverifyWorker._c_toolchain_gate`'s probe — `docker run … sh -c '…command -v
            # gcc…'` — which is not a bazel command at all and has no `.bazel` view. Recorded in
            # its OWN list so that every `.calls` assertion in this file keeps meaning "the bazel
            # commands the harness issued", and answered GREEN: these tests are about the
            # pipeline, and an imaginary image with no compiler would fail all of them for a
            # reason none of them is about. The refusal path is owned by `test_workers_build.py`,
            # against the worker directly.
            self.probes.append(call)
            return self._result(call, exit_code=0, stdout="/usr/bin/cc\n")
        self.calls.append(call)
        command = call.bazel
        if command[1] == "query":
            return self._query(call, command)
        return self._build_or_test(call, command)

    def _query(self, call: Invocation, command: tuple[str, ...]) -> ProcResult:
        text = command[-1]
        match = _DEST_IN_PATTERN.search(text)
        dest = "" if match is None else match.group(1)
        labels = list(self.closures.get(dest, ()))
        if text.rstrip().endswith(", 1)"):
            # `direct_rdeps_query` — depth 1. Every direct rdep survives sampling (§3.4), so the
            # table's first label stands in for "the change's immediate consumers".
            labels = labels[:1]
        return self._result(call, exit_code=0, stdout="".join(f"{label}\n" for label in labels))

    def _build_or_test(self, call: Invocation, command: tuple[str, ...]) -> ProcResult:
        pattern = next((arg for arg in command if arg.startswith("//")), "")
        dest = pattern.removeprefix("//").removesuffix("/...")
        code = self.fail.get((command[1], dest), 0)
        return self._result(
            call, exit_code=code, stdout="", stderr="" if code == 0 else LOUD_STDERR
        )

    def _result(
        self, call: Invocation, *, exit_code: int, stdout: str = "", stderr: str = ""
    ) -> ProcResult:
        self._seq += 1
        self.log_root.mkdir(parents=True, exist_ok=True)
        out_path = self.log_root / f"bazel-{self._seq}.out"
        err_path = self.log_root / f"bazel-{self._seq}.err"
        out_path.write_text(stdout, encoding="utf-8")
        err_path.write_text(stderr, encoding="utf-8")
        # The tails are handed over UNCAPPED on purpose. A runner that pre-truncated would hide
        # the guard that matters: `WorkerError.stderr_tail` is a `TruncatedStr`, and the
        # difference between truncating and rejecting is the difference between a persisted
        # build failure and an attempt that is never written at all (§7.1, models.base).
        return ProcResult(
            argv=call.argv,
            exit_code=exit_code,
            stdout_tail=stdout,
            stderr_tail=stderr,
            duration_ms=7,
            timed_out=False,
            stdout_bytes=len(stdout.encode()),
            stderr_bytes=len(stderr.encode()),
            stdout_path=out_path,
            stderr_path=err_path,
            cwd=call.cwd,
        )


# ---------------------------------------------------------------------------------------
# the fixture fleet, plus the monorepo Phase 3 merges into
# ---------------------------------------------------------------------------------------


MONOREPO_BAZEL_VERSION = "9.2.0"
"""The Bazel this suite actually runs, written into the fixture's `.bazelversion`.

**It used to say 7.4.1, and that pin was hiding a defect.** `tools/bin/bazel` is bazelisk and
bazelisk obeys `.bazelversion`, so the end-to-end pipeline was the one place in this suite
running a Bazel nobody has shipped against since — while `test_bazel.py`, with no `.bazelversion`
in its `tmp_path`, ran 9.2.0. The gap was not merely stale:

* `aspect_rules_js@3.x` declares `bazel_compatibility = [">=7.6.0"]`, so the pins in
  `build.ruleset_versions` cannot be loaded by 7.4.1 at all — the fixture and the config were
  describing two incompatible builds;
* and 7.4.1 still had `@local_config_platform`, which is exactly what `aspect_rules_ts@3.5.0`
  needed and Bazel 9 removed. The e2e test therefore could not see D8 no matter how broken the
  pin was, which is how a ruleset that cannot load under the shipped toolchain stayed pinned.

`test_the_fixture_runs_the_bazel_this_suite_verifies_against` asserts this string against what
the installed binary reports, so the two cannot drift apart again silently.
"""


def make_monorepo(workspace: Path) -> Path:
    """The monorepo at `run.monorepo_path`, with `integration` checked out and a root commit.

    Created by the test rather than by the harness on purpose: §3.3's precondition is that it
    already exists, and `fleet build` refuses with exit 2 rather than conjuring one — a verb that
    creates the repository it is supposed to merge into can never tell "misconfigured" from
    "first run".
    """
    path = workspace.parent / "acme-monorepo"
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-b", "integration")
    git(path, "config", "user.email", "fleet@example.invalid")
    git(path, "config", "user.name", "fleet")
    (path / ".bazelversion").write_text(f"{MONOREPO_BAZEL_VERSION}\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-m", "monorepo skeleton")
    return path


@pytest.fixture
def monorepo(fleet: Path) -> Path:  # noqa: F811  (`fleet` is the imported fixture)
    return make_monorepo(fleet)


@pytest.fixture
def filter_repo(monkeypatch: pytest.MonkeyPatch) -> FakeFilterRepo:
    fake = FakeFilterRepo()
    monkeypatch.setattr(cli, "FILTER_REPO_RUNNER", fake)
    return fake


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> FakeResolver:
    """`cli.RESOLVER_RUNNER`, installed for the life of one test.

    Requested by every test in this file that runs `fleet build` without a real Bazel. Without it
    the offline suite would resolve against PyPI and the npm registry on every run — which is not
    only slow but makes a green test depend on two package indexes being up.
    """
    fake = FakeResolver()
    monkeypatch.setattr(cli, "RESOLVER_RUNNER", fake)
    return fake


@pytest.fixture
def gazelle(monkeypatch: pytest.MonkeyPatch) -> FakeGazelle:
    """`cli.GAZELLE_RUNNER`, installed for the life of one test.

    Requested by the `bazel` fixture, so every test in this file that runs `fleet build` without
    a real Bazel has it. Without it a fleet containing a delegating ecosystem would execute the
    vendored generator for real — a Go toolchain, a module cache and, in the generator's default
    resolution mode, the network — inside the offline suite.
    """
    fake = FakeGazelle()
    monkeypatch.setattr(cli, "GAZELLE_RUNNER", fake)
    return fake


@pytest.fixture
def bazel(
    fleet: Path,  # noqa: F811
    filter_repo: FakeFilterRepo,
    resolver: FakeResolver,
    gazelle: FakeGazelle,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[FakeBazel]:
    """All four seams, installed for the life of one test and `None` again afterwards."""
    _ = (filter_repo, resolver, gazelle)
    fake = FakeBazel(fleet / "artifacts" / "fake-bazel")
    monkeypatch.setattr(cli, "BAZEL_RUNNER", fake)
    yield fake


def build(root: Path, *extra: str, json_output: bool = True) -> Any:
    argv = [*base_args(root)]
    if json_output:
        argv.append("--json")
    argv += ["build", *extra]
    return runner.invoke(app, argv, catch_exceptions=False)


def verify(root: Path, *extra: str, json_output: bool = True) -> Any:
    argv = [*base_args(root)]
    if json_output:
        argv.append("--json")
    argv += ["verify", *extra]
    return runner.invoke(app, argv, catch_exceptions=False)


def payload(result: Any) -> dict[str, Any]:
    return dict(json.loads(result.stdout))


def transformed(root: Path) -> None:
    """Phases 1 and 2, both asserted — a Phase 3 test that starts from a broken transform would
    report Phase 3's failure for Phase 2's reason."""
    scanned(root)
    assert transform(root).exit_code == ExitCode.SUCCESS


def build_worktree(root: Path, repo_id: str) -> Path:
    return root / "work" / "integration" / repo_id


def repo_ecosystem(root: Path, repo_id: str) -> Ecosystem:
    """The primary ecosystem Phase 1 recorded — the dispatch key §3.3 step 2 resolves on."""
    rows = query(root, "SELECT ecosystems FROM repos WHERE repo_id = ?", (repo_id,))
    declared = json.loads(str(rows[0][0] or "[]")) if rows else []
    return Ecosystem(declared[0]) if declared else Ecosystem.UNKNOWN


def relocations(filter_repo: FakeFilterRepo) -> dict[str, str]:
    """repo_id → the destination §3.3 step 1 rewrote its history onto, read off the real argv.

    Read from `git-filter-repo`'s own `--path-rename` rather than from `repos.dest_path`, because
    the column is NULL for every adapter-computed destination — which is precisely the case these
    tests exist to cover, and asserting on a NULL column would prove nothing about where the
    tree landed.
    """
    out: dict[str, str] = {}
    for call in filter_repo.calls:
        assert call.cwd is not None, call.argv
        rename = call.argv[call.argv.index("--path-rename") + 1]
        out[call.cwd.name] = rename.removeprefix(":").rstrip("/")
    return out


#: Four more real repositories, each declaring its language and NONE of them declaring a `dest:`.
#: The whole point is the missing `dest:`: it is what forces `layout()` through the adapter, so
#: `java/…`, `ts/…`, `py/…` and `misc/…` below are the ADAPTERS' answers and not the fixture's.
POLYGLOT_REPOS: dict[str, dict[str, str]] = {
    "acme-commons-java": {
        "pom.xml": (
            "<project>\n"
            "  <groupId>com.acme</groupId>\n"
            "  <artifactId>commons</artifactId>\n"
            "  <version>1.2.0</version>\n"
            "  <dependencies>\n"
            "    <dependency>\n"
            "      <groupId>com.google.guava</groupId>\n"
            "      <artifactId>guava</artifactId>\n"
            "      <version>33.2.1-jre</version>\n"
            "    </dependency>\n"
            "  </dependencies>\n"
            "</project>\n"
        ),
        "src/main/java/com/acme/commons/Widget.java": (
            "package com.acme.commons;\n\npublic final class Widget {}\n"
        ),
    },
    "acme-ui-ts": {
        "package.json": json.dumps(
            {"name": "@acme/ui", "version": "1.0.0", "dependencies": {"left-pad": "^1.3.0"}},
            indent=2,
        ),
        "src/index.ts": "export const version = '1.0.0';\n",
    },
    "acme-tool-py": {
        "pyproject.toml": (
            "[project]\n"
            'name = "acme-tool-py"\n'
            'version = "0.3.0"\n'
            'dependencies = ["requests>=2.31"]\n'
        ),
        "acme_tool_py/__init__.py": "VERSION = '0.3.0'\n",
    },
    #: The SECOND JS repo of the fleet, and the ONE thing that matters about it is that its
    #: external npm dependency is a DIFFERENT package from the one `acme-app-ts` declares
    #: (`left-pad`). `acme-ui-ts` above is also npm, but it declares `left-pad` too — so both
    #: repos resolve to the same root `pnpm-lock.yaml` content and the fleet had never had two
    #: JS repos whose locks disagree. This one does, which is what made §17's defect reachable —
    #: and what now keeps ADR-0048's one-importer-per-repo lock honest.
    #:
    #: Shaped exactly like `acme-lib-ts` (a `src/index.ts` the JS adapter recognizes as an
    #: entrypoint, one exported symbol) plus the dependency, and it deliberately does NOT
    #: `import` the package: `acme-app-ts` declares `left-pad` and imports it nowhere either, so
    #: the generated `deps = ["//ts/acme/report:node_modules/ms"]` is the whole of the claim.
    #: That label is resolved at ANALYSIS time, so the failure is Bazel's answer about the root
    #: lock rather
    #: than `tsc`'s answer about a package's bundled `.d.ts` — which would make the test's verdict
    #: depend on how a registry package ships its types.
    "acme-report-ts": {
        "package.json": json.dumps(
            {"name": "@acme/report", "version": "0.1.0", "dependencies": {"ms": "^2.1.3"}},
            indent=2,
        ),
        "src/index.ts": "export const label = 'report';\n",
    },
    #: The SECOND Python repo that carries an external dependency, and — exactly as with
    #: `acme-report-ts` above — the ONE thing that matters is that the distribution is a
    #: DIFFERENT one from what `acme-app-py` declares (`requests`). `acme-tool-py` is also PyPI
    #: but declares `requests` too, so both units render the identical `requirements.in` and the
    #: fleet had never had two Python repos whose root locks disagree.
    #:
    #: `jinja2` is chosen for the same reason `certifi` is the assertion in
    #: `test_the_resolved_lock_carries_the_transitive_closure`: nothing else in this fleet names
    #: it, and its own transitive dependency `markupsafe` can appear in the root lock only if a
    #: resolver read `jinja2`'s metadata. So the generated `//:requirements.lock` names all four
    #: of `requests`, `certifi`, `jinja2` and `markupsafe` iff the resolve really was over the
    #: UNION of the fleet's Python units — and names one repo's half of them if it was not.
    #:
    #: Shaped exactly like `acme-tool-py` (a package directory with one module, no entrypoint the
    #: adapter recognizes), and it deliberately does NOT `import jinja2`: `acme-app-py` declares
    #: `requests` and imports it nowhere either, so `deps = ["@pypi//jinja2"]` is the whole of the
    #: claim. That label is resolved at ANALYSIS time, so a missing distribution is Bazel's answer
    #: about the root lock rather than a runtime `ImportError` inside a `py_binary` — which would
    #: make the verdict depend on the wheel actually being importable on this interpreter.
    "acme-metrics-py": {
        "pyproject.toml": (
            "[project]\n"
            'name = "acme-metrics-py"\n'
            'version = "0.4.0"\n'
            'dependencies = ["jinja2>=3.1"]\n'
        ),
        "acme_metrics_py/__init__.py": "VERSION = '0.4.0'\n",
    },
    #: The FIRST of the fleet's two Rust repos — before these two, `docs/PROGRESS.md` §21 recorded
    #: **zero** Rust fixture repos and nothing in this project had ever compiled a line of Rust.
    #: They are added as a PAIR because one of them proves nothing: `rust.py:workspace_files`
    #: renders the root `[workspace]` from every contributing unit and keeps the root
    #: `Cargo.lock`'s `carry_from`, and both of those decisions are only observable with two
    #: members and two locks that disagree — exactly the shape `acme-report-ts` gave npm and
    #: `acme-metrics-py` gave PyPI.
    #:
    #: **These two ARE driven through `fleet build` now**, by
    #: `test_two_rust_repos_in_one_wave_both_build` (real Bazel, real `git-filter-repo`, real
    #: `bazel build //...` over the integration branch) and, offline, by
    #: `test_every_dispatched_worktree_carries_every_dest_the_root_files_were_computed_over`.
    #: They could not be, and the reason was in the Phase 3 driver rather than in `rust.py`:
    #:
    #: **The fleet-wide `members` list named a directory the per-repo snapshot did not have.**
    #: Phase 3 used to cut each repo's build worktree from the integration snapshot taken at *its
    #: own* merge, so `rust/acme-codec-rs/` did not exist yet while `//rust/acme-case-rs/...` was
    #: being built — and cargo fails the WHOLE workspace on a member it cannot read:
    #: `error: failed to load manifest for workspace member '…/rust/acme-codec-rs' referenced by
    #: workspace at '…/Cargo.toml' … No such file or directory (os error 2)`, surfacing as
    #: `Error: Failed to generate lockfile` out of `crate_universe`'s splicer. The first Rust repo
    #: in the sequence failed 3/3 attempts and the last one built green. JS escaped this only
    #: because `npm_translate_lock` tolerates a pnpm importer whose directory is absent, and
    #: Python because a requirements file names distributions, not paths. The driver now cuts ONE
    #: snapshot per WAVE, after the wave's last ingest, so every member of a wave builds in a tree
    #: that holds every member — which is what the two tests above assert, the second of them
    #: without Bazel at all.
    #:
    #: **The other half was `crate.from_cargo` REWRITING `//:Cargo.lock` in the worktree** (it
    #: writes the extended lock back to the source tree) while `_publish`'s `git add` commits
    #: every support-file path — so the one repo that did build published different bytes for a
    #: fleet-wide root file than every other repo did, and its integration merge failed
    #: `CONFLICT (add/add): Merge conflict in Cargo.lock`. `_publish` re-asserts the PLANNED bytes
    #: over the file before staging, which is what closed it (`rust.py:workspace_files` documents
    #: the same decision from the adapter's end).
    #:
    #: **This fixture pair proves the INTRA-wave case only.** Neither repo declares an internal
    #: dependency, so §3.1 step 7 puts both in one wave. Two Rust repos in DIFFERENT waves
    #: reproduce the failure above verbatim: the earlier wave settles with root files computed
    #: over a smaller domain and is never re-admitted. That residue is out of scope here and no
    #: test in this file claims otherwise.
    #:
    #: **`hex` and `heck` are the crates, and the choice is load-bearing in three ways.** Each is
    #: named by exactly one repo and by nothing else in the fleet, so the root `Cargo.lock` really
    #: does have two different candidate contents. Each has **zero** dependencies of its own and
    #: **no build script**, so `crate.from_cargo` fetches two `.crate` files and generates two
    #: `rust_library` targets rather than a transitive tree — the build stays about the harness's
    #: workspace rather than about how fast crates.io serves a hundred crates. And the two locks
    #: are asymmetric under `union_workspace_files`' total order: `rust/acme-case-rs` sorts before
    #: `rust/acme-codec-rs`, so the root lock is seeded from `acme-case-rs`, which names `heck` and
    #: **not** `hex`. A monorepo in which `@crates//:hex` resolves is therefore a monorepo in which
    #: cargo really extended the carried lock, which is the claim `rust.py`'s "the lock KEEPS its
    #: `carry_from`" docstring makes and which no single-Rust-repo fleet could have tested.
    #:
    #: **These DO import their dependency, and that is a deliberate departure from the JS/Python
    #: precedent above.** `acme-report-ts` and `acme-metrics-py` declare and never import, because
    #: importing would make the verdict depend on how a registry package ships its `.d.ts` or
    #: whether a wheel is importable on this interpreter — failure modes outside the harness.
    #: Rust has no such extra surface: `rules_rust` links a `deps` entry by passing
    #: `--extern <crate_name>=<path>` to the SAME `rustc` invocation that compiles `src/lib.rs`,
    #: over an rlib built from the crate's own source by the same toolchain in the same action
    #: graph. An unused `--extern` is not an error, so declaring alone would prove only that the
    #: label resolved at analysis time and that the rlib was an action input; `hex::encode` and
    #: `heck::ToSnakeCase` additionally force `rustc` to RESOLVE the extern crate name and read
    #: the rlib's metadata. That is strictly more, at no cost in new failure modes — so here the
    #: dependency is made real at build time rather than only at analysis time. It is also what
    #: makes the emission non-vacuous: a `generate_targets()` that returned `[]` would leave
    #: `bazel build //...` green over a tree in which nothing ever compiled, and an rlib on disk
    #: cannot be produced that way.
    #:
    #: `Cargo.lock` is shipped because `rust.py` declares **no `resolution()`**: nothing in this
    #: harness resolves Cargo, and `_EMPTY_CARGO_LOCK` (`version = 3`, no `[[package]]`) is the
    #: floor a repo that carries none falls back to. A hand-written lock would be a fixture
    #: asserting against itself, so both were generated by the workspace's own pinned toolchain —
    #: `tools/bin/cargo generate-lockfile` (cargo 1.97.1), whose `version = 4` and `checksum` lines
    #: below are its output verbatim.
    "acme-codec-rs": {
        "Cargo.toml": (
            "[package]\n"
            'name = "acme-codec-rs"\n'
            'version = "0.1.0"\n'
            'edition = "2021"\n'
            "\n"
            "[dependencies]\n"
            'hex = "0.4"\n'
        ),
        "src/lib.rs": ("pub fn encode(bytes: &[u8]) -> String {\n    hex::encode(bytes)\n}\n"),
        "Cargo.lock": (
            "# This file is automatically @generated by Cargo.\n"
            "# It is not intended for manual editing.\n"
            "version = 4\n"
            "\n"
            "[[package]]\n"
            'name = "acme-codec-rs"\n'
            'version = "0.1.0"\n'
            "dependencies = [\n"
            ' "hex",\n'
            "]\n"
            "\n"
            "[[package]]\n"
            'name = "hex"\n'
            'version = "0.4.3"\n'
            'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
            'checksum = "7f24254aa9a54b5c858eaee2f5bccdb46aaf0e486a595ed5fd8f86ba55232a70"\n'
        ),
    },
    #: The SECOND Rust repo. Everything the entry above says applies here; what is specific to this
    #: one is that its `dest` (`rust/acme-case-rs`) sorts FIRST, so it is this repo's `Cargo.lock`
    #: that seeds the monorepo root and `acme-codec-rs`'s `hex` that has to be added to it by
    #: cargo. `edition = "2021"` on both, matching `rust.py:_EDITION` — `cargo init` would have
    #: written `2024`, and a member manifest whose edition disagrees with the `rust_library`'s
    #: `edition` attribute is two different compilers' opinions of one crate.
    "acme-case-rs": {
        "Cargo.toml": (
            "[package]\n"
            'name = "acme-case-rs"\n'
            'version = "0.2.0"\n'
            'edition = "2021"\n'
            "\n"
            "[dependencies]\n"
            'heck = "0.5"\n'
        ),
        "src/lib.rs": (
            "use heck::ToSnakeCase;\n"
            "\n"
            "pub fn slug(name: &str) -> String {\n"
            "    name.to_snake_case()\n"
            "}\n"
        ),
        "Cargo.lock": (
            "# This file is automatically @generated by Cargo.\n"
            "# It is not intended for manual editing.\n"
            "version = 4\n"
            "\n"
            "[[package]]\n"
            'name = "acme-case-rs"\n'
            'version = "0.2.0"\n'
            "dependencies = [\n"
            ' "heck",\n'
            "]\n"
            "\n"
            "[[package]]\n"
            'name = "heck"\n'
            'version = "0.5.0"\n'
            'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
            'checksum = "2304e00983f87ffb38b55b444b5e3b60a884b5d30c0fca7d82fe33449bbe55ea"\n'
        ),
    },
    #: The FIRST of the fleet's two Go repos. Before these two, `go.py` — the ONE delegating
    #: adapter — had never been exercised by a repository: every Go assertion in this file built a
    #: `BuildUnit` by hand, so `manifests/gomod.py`'s parse, the adapter's `layout()`, and the
    #: fleet-wide union of §3.3 step 2 had never met a real `go.mod` on a real branch.
    #:
    #: **They are a PAIR for `go.py:workspace_files`' reason and no other.** `go_deps.from_file`
    #: names exactly ONE `//:go.mod` at the monorepo root, so the union that file is — and the
    #: ADR-0050 step 2 defect where `union_workspace_files`' first-writer-wins `setdefault` kept
    #: one repo's requirements and silently dropped the next repo's — is observable only with two
    #: Go repos whose `require` blocks are disjoint. That is the same shape `acme-report-ts` gave
    #: npm, `acme-metrics-py` gave PyPI and the two `*-rs` repos gave Cargo.
    #:
    #: **`cobra` and `x/crypto` are the dependencies, and the pair is load-bearing twice.** Each is
    #: named by exactly one of the two repos and by nothing else in the fleet, so the root `go.mod`
    #: really does have two candidate contents. And they exercise the two DIFFERENT label-naming
    #: schemes `go_deps` produces, which `go.py:_bazel_repo_name` reimplements and which a
    #: one-dependency fixture could never separate: a `github.com` module reverses its host into
    #: `@com_github_spf13_cobra`, while `golang.org/x/crypto` — whose host has no `www`-style third
    #: element and whose path carries the `x/` vanity segment — becomes `@org_golang_x_crypto`.
    #: Both are at the top of a 25-repo corpus survey of real Go projects, so this is the shape a
    #: migration actually meets.
    #:
    #: **Both fixtures IMPORT what they declare, and in Go that is not a stylistic choice.** The
    #: JS and Python fixtures above deliberately declare-and-never-import; Go cannot do that at
    #: all — an unused import is a COMPILE ERROR (`imported and not used`), so a `.go` file naming
    #: a module it does not use is not a Go file. It also would not test anything: Gazelle derives
    #: `deps` purely from `import` statements, so a declared-but-unimported dependency yields an
    #: empty `deps` and the label-naming schemes above would never be reached. This inverts the
    #: JS/Python convention and it is also not the Rust reasoning (an unused `--extern` is legal
    #: there and importing was an upgrade); here it is forced by the language.
    #:
    #: **`go 1.24.12`, and never a 1.26.x line copied from a real repo.** The vendored
    #: `tools/bin/go` pins `GOTOOLCHAIN` to the exact SDK version, so a `go.mod` declaring a
    #: HIGHER version is `toolchain not available` rather than a silent toolchain download — which
    #: is the point of the pin and is why a real repo's `go` line cannot be copied verbatim. It is
    #: the same constant `go.py:_GO_VERSION` registers for the `go_sdk` toolchain, so the fixture
    #: and the SDK cannot drift. It reads 1.24.12 rather than 1.23.4 because the pinned gazelle
    #: (0.52.2, `go.work` floor `>= 1.24.12`) and rules_go (0.61.1, `go.mod` `go 1.24.0`) are both
    #: above 1.23.4 and `go_deps` cannot build its BUILD-file tools under it.
    #:
    #: **The `go.sum` is REAL and is generated, not written.** `tools/bin/go mod tidy` produced the
    #: `// indirect` block below (a module using cobra genuinely requires `pflag` and `mousetrap`,
    #: and `manifests/gomod.py` keeps indirect requirements on purpose — they are edges), then
    #: `rm go.sum && tools/bin/go mod download all` — the EXACT argv `go.py:_RESOLVER` declares —
    #: wrote the sums below verbatim, leaving `go.mod` byte-identical (`-mod=readonly`).
    #: `tools/bin/go build ./...` compiles both fixtures. The sums are a DECOY at the fleet level:
    #: they are hashes taken against THIS repo's `go.mod`, so promoting them to the monorepo root
    #: beside a unioned `go.mod` would be a checksum mismatch, and `go.py` drops `carry_from` for
    #: exactly that reason. Their job here is to be a real candidate that must lose.
    #:
    #: This one is the BINARY-shaped layout of the corpus survey — `cmd/<name>/main.go` plus an
    #: `internal/` package it imports — which is what makes `# gazelle:prefix` load-bearing:
    #: `cmd/clitool` resolves `github.com/acme/clitool/internal/command` only through the prefix.
    "acme-clitool-go": {
        "go.mod": (
            "module github.com/acme/clitool\n"
            "\n"
            "go 1.24.12\n"
            "\n"
            "require github.com/spf13/cobra v1.8.1\n"
            "\n"
            "require (\n"
            "\tgithub.com/inconshreveable/mousetrap v1.1.0 // indirect\n"
            "\tgithub.com/spf13/pflag v1.0.5 // indirect\n"
            ")\n"
        ),
        "go.sum": (
            "github.com/cpuguy83/go-md2man/v2 v2.0.4 h1:"
            "wfIWP927BUkWJb2NmU/kNDYIBTh/ziUX91+lVfRxZq4=\n"
            "github.com/cpuguy83/go-md2man/v2 v2.0.4/go.mod h1:"
            "tgQtvFlXSQOSOSIRvRPT7W67SCa46tRHOmNcaadrF8o=\n"
            "github.com/inconshreveable/mousetrap v1.1.0 h1:"
            "wN+x4NVGpMsO7ErUn/mUI3vEoE6Jt13X2s0bqwp9tc8=\n"
            "github.com/inconshreveable/mousetrap v1.1.0/go.mod h1:"
            "vpF70FUmC8bwa3OWnCshd2FqLfsEA9PFc4w1p2J65bw=\n"
            "github.com/russross/blackfriday/v2 v2.1.0 h1:"
            "JIOH55/0cWyOuilr9/qlrm0BSXldqnqwMsf35Ld67mk=\n"
            "github.com/russross/blackfriday/v2 v2.1.0/go.mod h1:"
            "+Rmxgy9KzJVeS9/2gXHxylqXiyQDYRxCVz55jmeOWTM=\n"
            "github.com/spf13/cobra v1.8.1 h1:e5/vxKd/rZsfSJMUX1agtjeTDf+qv1/JdBF8gg5k9ZM=\n"
            "github.com/spf13/cobra v1.8.1/go.mod h1:wHxEcudfqmLYa8iTfL+OuZPbBZkmvliBWKIezN3kD9Y=\n"
            "github.com/spf13/pflag v1.0.5 h1:iy+VFUOCP1a+8yFto/drg2CJ5u0yRoB7fZw3DKv/JXA=\n"
            "github.com/spf13/pflag v1.0.5/go.mod h1:McXfInJRrz4CZXVZOBLb0bTZqETkiAhM9Iw0y3An2Bg=\n"
            "gopkg.in/check.v1 v0.0.0-20161208181325-20d25e280405 h1:"
            "yhCVgyC4o1eVCa2tZl7eS0r+SDo693bJlVdllGtEeKM=\n"
            "gopkg.in/check.v1 v0.0.0-20161208181325-20d25e280405/go.mod h1:"
            "Co6ibVJAznAaIkqp8huTwlJQCZ016jof/cbN4VW5Yz0=\n"
            "gopkg.in/yaml.v3 v3.0.1 h1:fxVm/GzAzEWqLHuvctI91KS9hhNmmWOoWu0XTYJS7CA=\n"
            "gopkg.in/yaml.v3 v3.0.1/go.mod h1:K4uyk7z7BCEPqu6E+C64Yfv1cQ7kz7rIZviUmN+EgEM=\n"
        ),
        "internal/command/root.go": (
            "package command\n"
            "\n"
            'import "github.com/spf13/cobra"\n'
            "\n"
            "// Root is the clitool command tree.\n"
            "func Root() *cobra.Command {\n"
            '\treturn &cobra.Command{Use: "clitool", Short: "acme clitool"}\n'
            "}\n"
        ),
        "cmd/clitool/main.go": (
            "package main\n"
            "\n"
            'import "github.com/acme/clitool/internal/command"\n'
            "\n"
            "func main() {\n"
            "\t_ = command.Root().Execute()\n"
            "}\n"
        ),
    },
    #: The SECOND Go repo. Everything the entry above says applies here; what is specific to this
    #: one is the LAYOUT and the label scheme. It is the corpus's other common shape — a flat,
    #: single-package library with its one `.go` file at the module root and no `cmd/` or
    #: `internal/` at all — so between the two fixtures the adapter's `layout()` is exercised over
    #: both a nested tree and a bare one, and `golang.org/x/crypto` is what makes
    #: `@org_golang_x_crypto` (rather than a second `@com_github_…`) reachable.
    #:
    #: Its `dest` (`go/digest`) sorts AFTER `go/clitool`, so under any first-writer-wins union it
    #: is THIS repo's requirements that would be dropped — which is why the assertions below name
    #: `golang.org/x/crypto` explicitly as well as asserting the whole union.
    "acme-digest-go": {
        "go.mod": (
            "module github.com/acme/digest\n"
            "\n"
            "go 1.24.12\n"
            "\n"
            "require golang.org/x/crypto v0.31.0\n"
            "\n"
            "require golang.org/x/sys v0.28.0 // indirect\n"
        ),
        "go.sum": (
            "golang.org/x/crypto v0.31.0 h1:ihbySMvVjLAeSH1IbfcRTkD/iNscyz8rGzjF/E5hV6U=\n"
            "golang.org/x/crypto v0.31.0/go.mod h1:kDsLvtWBEx7MV9tJOj9bnXsPbxwJQ6csT/x4KIN4Ssk=\n"
            "golang.org/x/net v0.21.0 h1:AQyQV4dYCvJ7vGmJyKki9+PBdyvhkSd8EIx/qb0AYv4=\n"
            "golang.org/x/net v0.21.0/go.mod h1:bIjVDfnllIU7BJ2DNgfnXvpSvtn8VRwhlsaeUTyUS44=\n"
            "golang.org/x/sys v0.28.0 h1:Fksou7UEQUWlKvIdsqzJmUmCX3cZuD2+P3XyyzwMhlA=\n"
            "golang.org/x/sys v0.28.0/go.mod h1:/VUhepiaJMQUp4+oa/7Zr1D23ma6VTLIYjOOTFZPUcA=\n"
            "golang.org/x/term v0.27.0 h1:WP60Sv1nlK1T6SupCHbXzSaN0b9wUmsPoRS9b61A23Q=\n"
            "golang.org/x/term v0.27.0/go.mod h1:iMsnZpn0cago0GOrHO2+Y7u7JPn5AylBrcoWkElMTSM=\n"
            "golang.org/x/text v0.21.0 h1:zyQAAkrwaneQ066sspRyJaG9VNi/YJ1NfzcGB3hZ/qo=\n"
            "golang.org/x/text v0.21.0/go.mod h1:4IBbMaMmOPCJ8SecivzSH54+73PCFmPWxNTLm+vZkEQ=\n"
        ),
        "digest.go": (
            "package digest\n"
            "\n"
            'import "golang.org/x/crypto/blake2b"\n'
            "\n"
            "// Sum returns the BLAKE2b-256 digest of payload.\n"
            "func Sum(payload []byte) []byte {\n"
            "\tsum := blake2b.Sum256(payload)\n"
            "\treturn sum[:]\n"
            "}\n"
        ),
    },
    #: No manifest any adapter recognizes ⇒ `Ecosystem.UNKNOWN` ⇒ the §3.1 step 2 floor.
    "acme-runbooks": {"README.txt": "on-call runbooks\n", "notes/oncall.txt": "page someone\n"},
    #: D112: every OTHER Python fixture in this file (and in `test_scan_e2e.FIXTURE_REPOS`) ships
    #: zero test files, so `test_sources()` has always returned `[]` for every real-Bazel run this
    #: suite has ever done — the gap `D112` in `docs/INTEGRATION_HONESTY.md` describes. This is
    #: the first Python fixture with a real `test_*.py`, named by pytest's own default discovery
    #: convention (`docs/INTEGRATION_HONESTY.md`'s `## D112` entry, `_is_python_test_src` in
    #: `cli.py`). The assertion is a bare module-level `assert`, not a `pytest`-collected
    #: function: `py.py`'s `test_targets()` sets `main=test_srcs[0]` and no `deps` on a test
    #: framework, so a real `bazel test` executes this file as a plain script — a `def test_…():`
    #: never called would make the target "pass" vacuously and prove nothing about a real defect
    #: failing it.
    "acme-widgets-py": {
        "pyproject.toml": (
            "[project]\nname = \"acme-widgets-py\"\nversion = \"0.1.0\"\ndependencies = []\n"
        ),
        "acme_widgets_py/__init__.py": (
            "def double(value: int) -> int:\n    return value * 2\n"
        ),
        # Self-contained rather than `from acme_widgets_py import double`: `main`'s own directory
        # and the package's `imports = ["."]` root would BOTH be on `sys.path` under a real
        # `py_test` runfiles tree, and this fixture has no need to find out whether that
        # resolves the same module twice or not. What must be real is the ASSERT: a bare
        # top-level statement, not a `def test_…():` `pytest` would collect but `py_test`'s
        # `main=` (no `deps` on a test framework — `py.py:test_targets()`) never calls.
        "acme_widgets_py/test_widgets.py": (
            "def _double(value: int) -> int:\n"
            "    return value * 2\n"
            "\n"
            "\n"
            "assert _double(21) == 42\n"
        ),
    },
    #: D112, round VI task 70: `acme-commons-java` above (like every other JVM fixture in this
    #: file) ships zero `src/test/java` files, so `test_sources()` has always returned `[]` for
    #: every real-Bazel run this suite has ever done, exactly as `acme-widgets-py` was for Python.
    #: This is the first JVM fixture with a real test class, named by Maven/Gradle's own
    #: `src/test/java` convention (`docs/INTEGRATION_HONESTY.md`'s `## D112` entry,
    #: `_is_jvm_test_src` in `cli.py`). Unlike the Python/JS precedent, `jvm.py`'s `test_targets()`
    #: emits a plain `java_test(srcs=test_srcs, deps=[lib, *external_labels])` with no `attrs` at
    #: all — the default `use_testrunner=True` JUnit4 runner — so the test class needs a REAL
    #: `junit:junit` dependency reachable on the classpath, declared with Maven's own `<scope>test
    #: </scope>` (kept by `_external_coordinates`/`workspace_deps`: nothing in this harness filters
    #: dependencies by scope — see `graph/infer.py`'s `TEST_SCOPES` check, which only ever gates
    #: INTERNAL dependency-edge inference, not `BuildUnit.external_coordinates`).
    "acme-widgets-jvm": {
        "pom.xml": (
            "<project>\n"
            "  <groupId>com.acme</groupId>\n"
            "  <artifactId>widgets</artifactId>\n"
            "  <version>0.1.0</version>\n"
            "  <dependencies>\n"
            "    <dependency>\n"
            "      <groupId>junit</groupId>\n"
            "      <artifactId>junit</artifactId>\n"
            "      <version>4.13.2</version>\n"
            "      <scope>test</scope>\n"
            "    </dependency>\n"
            "  </dependencies>\n"
            "</project>\n"
        ),
        "src/main/java/com/acme/widgets/Widget.java": (
            "package com.acme.widgets;\n"
            "\n"
            "public final class Widget {\n"
            "    public static int doubleIt(int value) {\n"
            "        return value * 2;\n"
            "    }\n"
            "}\n"
        ),
        # A real JUnit4 `@Test`, not a bare `main`: unlike `py_test`'s `main=`/no-framework shape,
        # `jvm.py:test_targets()` sets no `attrs` at all, so the target keeps `java_test`'s DEFAULT
        # `use_testrunner=True` and expects a JUnit-style test class — a `main` method would never
        # be invoked by the bundled JUnit4 runner and would make the target "pass" vacuously.
        "src/test/java/com/acme/widgets/WidgetTest.java": (
            "package com.acme.widgets;\n"
            "\n"
            "import org.junit.Test;\n"
            "import static org.junit.Assert.assertEquals;\n"
            "\n"
            "public final class WidgetTest {\n"
            "    @Test\n"
            "    public void doublesTwentyOne() {\n"
            "        assertEquals(42, Widget.doubleIt(21));\n"
            "    }\n"
            "}\n"
        ),
    },
    #: D112, round VI task 87: `acme-ui-ts`/`acme-report-ts` above (like every other JS fixture in
    #: this file) ship zero `*.test.ts` files, so `test_sources()` has always returned `[]` for
    #: every real-Bazel run this suite has ever done, exactly as `acme-widgets-py`/`-jvm` were for
    #: Python/JVM. This is the first JS fixture with a real test file, named by the Jest-style
    #: `*.test.ts` convention this task decided (`docs/INTEGRATION_HONESTY.md`'s `## D112` entry,
    #: `_is_js_test_src` in `cli.py`). Zero declared dependencies, deliberately: `js.py`'s
    #: `test_targets()` now emits a SECOND `ts_project` (`{name}_test_lib`) to compile the test
    #: file before `js_test` can name its output as `entry_point` — a repo with an `@npm` hub would
    #: exercise `_needs_npm_hub`/`workspace_deps` too, which is D10/D12 territory this fixture has
    #: no need to re-prove.
    #:
    #: **The `__tests__/` pair is not decoration (round VI task 87 review, fix round 1).** Jest's
    #: default layout puts helpers and `__snapshots__/*.snap` under `__tests__/`, and
    #: `_is_js_test_src` is the one predicate keyed on a DIRECTORY rather than a basename — so
    #: this is the only fixture shape that can express either bug the review found: the `.snap`
    #: being silently dropped from the BUILD file entirely (it must stay carried as the library
    #: `ts_project`'s `data`), and `entry_point` picking the alphabetically-first path — which
    #: `__tests__/helper.ts` IS, since `package_relative` sorts and `_` (0x5F) < `s` (0x73) —
    #: instead of the actual test. Remove either file and the covering test below certifies both
    #: defects green.
    "acme-widgets-ts": {
        "package.json": json.dumps(
            {"name": "acme-widgets-ts", "version": "0.1.0", "dependencies": {}}, indent=2
        ),
        "src/index.ts": (
            "export function double(value: number): number {\n  return value * 2;\n}\n"
        ),
        "__tests__/helper.ts": "export const EXPECTED = 42;\n",
        "__tests__/__snapshots__/index.test.ts.snap": "exports[`double doubles 21`] = `42`;\n",
        "src/index.test.ts": (
            "import { double } from './index';\n"
            "\n"
            "if (double(21) !== 42) {\n"
            "  throw new Error('double(21) !== 42');\n"
            "}\n"
        ),
    },
}


def add_repos(root: Path, names: Sequence[str]) -> None:
    """Append real git repositories to the fixture fleet's manifest, with **no `dest:`**.

    Called before `fleet scan`, so everything downstream — the inventory, the graph, the waves,
    the relocation — is the shipped code reacting to a bigger fleet rather than a fixture reaching
    into the database.
    """
    sources = root.parent / "sources"
    entries = "".join(
        f"  - name: {name}\n    url: {_make_repo(sources, name, POLYGLOT_REPOS[name])}\n"
        for name in names
    )
    manifest = root / "config" / "repos.yaml"
    manifest.write_text(manifest.read_text(encoding="utf-8") + entries, encoding="utf-8")


def only_repos(root: Path, names: Sequence[str]) -> None:
    """Replace the fixture fleet's manifest with EXACTLY these repositories.

    `add_repos` grows the fleet; this bounds it, and the difference is load-bearing for any test
    whose claim is about the whole monorepo. Since ADR-0055 the fleet-wide root files describe
    every ELIGIBLE unit whatever `--repo` says — the flag narrows dispatch and never the domain —
    so a `bazel build //...` over the integration branch is a fair question only of a fleet whose
    every unit was actually dispatched and published. Bounding the fleet keeps it fair and keeps
    the disk bill down; bounding the dispatch would merely leave the branch half-generated, with
    root files correctly naming packages that no invocation ever wrote.
    """
    sources = root.parent / "sources"
    entries = "".join(
        f"  - name: {name}\n    url: {_make_repo(sources, name, POLYGLOT_REPOS[name])}\n"
        for name in names
    )
    (root / "config" / "repos.yaml").write_text(
        f"version: 1\ndefaults:\n  ref: main\nrepos:\n{entries}", encoding="utf-8"
    )


def verify_worktree(root: Path, repo_id: str) -> Path:
    return root / "work" / "verify" / repo_id


def merge_trailers(monorepo: Path) -> list[tuple[str, str]]:
    """`(Source-Repo, Source-Sha)` for every ADR-0011 merge on `integration`, oldest first."""
    fmt = (
        "%(trailers:key=Source-Repo,valueonly,separator=%x2c)%x1f"
        "%(trailers:key=Source-Sha,valueonly,separator=%x2c)%x1e"
    )
    raw = git(monorepo, "log", f"--format={fmt}", "integration")
    out: list[tuple[str, str]] = []
    for record in raw.split("\x1e"):
        repo, sep, sha = record.strip("\n").partition("\x1f")
        if sep and repo.strip():
            out.append((repo.strip(), sha.strip()))
    return list(reversed(out))


def attempts(root: Path, phase: int) -> list[dict[str, Any]]:
    rows = query(
        root,
        "SELECT repo_id, attempt, command, exit_code, stderr_tail, integration_ref, "
        "       failure_class FROM attempts WHERE phase = ? ORDER BY repo_id, attempt",
        (phase,),
    )
    return [
        {
            "repo_id": row[0],
            "attempt": row[1],
            "command": json.loads(str(row[2])),
            "exit_code": row[3],
            "stderr_tail": row[4] or "",
            "integration_ref": row[5],
            "failure_class": row[6],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------------------
# 1. the chain reaches Phase 3, and the generated files are really there
# ---------------------------------------------------------------------------------------


def test_build_reaches_phase_three_and_lands_the_generated_files(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    filter_repo: FakeFilterRepo,
) -> None:
    """`scan → sequence → transform → build` exits 0, every repo's Phase 3 row is `SUCCEEDED`,
    and the two generated files exist in the worktree the build read.

    Why on disk and not on `attempts`: every layer between the plan and the file — the target
    list, `render_build_bazel`, the worktree cut from the snapshot — can succeed against a tree
    nobody wrote. A database recording a `BUILD.bazel` that is not there is precisely what an
    end-to-end test is for, and it is also what Phase 4 would then try to build.

    The merge itself is asserted through git rather than through SQLite because ADR-0011 put the
    provenance in the commit: `Source-Repo:`/`Source-Sha:` are answerable from a bare clone with
    the database deleted, and nothing else in the harness can say where a merged file came from.
    """
    transformed(fleet)
    result = build(fleet, "--no-sandbox")
    assert result.exit_code == ExitCode.SUCCESS, result.output

    statuses = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 3"))
    assert set(statuses) == set(DESTINATIONS), statuses
    assert set(statuses.values()) == {"SUCCEEDED"}, statuses

    # One merge per repo, carrying the origin SHA Phase 2 actually left on `migrate/<repo>`.
    merged = dict(merge_trailers(monorepo))
    assert set(merged) == set(DESTINATIONS), merged
    for repo_id in DESTINATIONS:
        tip = git(fleet / "work" / repo_id, "rev-parse", f"migrate/{repo_id}")
        assert merged[repo_id] == tip, repo_id

    ecosystems.discover()
    for repo_id, dest in DESTINATIONS.items():
        tree = build_worktree(fleet, repo_id)
        build_file = tree / dest / "BUILD.bazel"
        assert build_file.is_file(), f"{repo_id}: no BUILD.bazel at {build_file}"
        body = build_file.read_text(encoding="utf-8")
        # The REGISTERED adapter's shape, read off the registry rather than spelled here: what
        # this asserts is that the rule in the file is the one the unit's own adapter names, not
        # the §3.1 step 2 floor every repo used to get.
        adapter = ecosystems.for_ecosystem(repo_ecosystem(fleet, repo_id))
        assert not adapter.degraded, f"{repo_id}: no adapter claimed a recognized ecosystem"
        assert f"{adapter.library_rule}(" in body, body
        assert "filegroup(" not in body, body
        assert f'load("{adapter.library_bzl}"' in body, body
        assert f'name = "{dest.rsplit("/", maxsplit=1)[-1]}"' in body, body
        assert '"package.json"' in body or '"pyproject.toml"' in body, body
        assert "DO NOT EDIT" in body.upper() or "generated" in body.lower(), body

        module = tree / "MODULE.bazel"
        assert module.is_file(), f"{repo_id}: no MODULE.bazel at {module}"
        assert 'name = "monorepo"' in module.read_text(encoding="utf-8")

    # §3.3 step 1's history rewrite, as the argv it WOULD have executed: one invocation per
    # repo, against the throwaway clone, renaming the repo root onto `<dest>/` and stripping the
    # oversize blobs §3.2 step 1 keeps out of monorepo history.
    assert len(filter_repo.calls) == len(DESTINATIONS), filter_repo.calls
    for call in filter_repo.calls:
        assert call.argv[0] == "git-filter-repo", call.argv
        rename = call.argv[call.argv.index("--path-rename") + 1]
        assert rename.startswith(":"), rename
        assert rename.removeprefix(":").rstrip("/") in set(DESTINATIONS.values()), rename
        assert "--strip-blobs-bigger-than" in call.argv, call.argv
        assert call.cwd is not None and "ingest" in call.cwd.parts, call.cwd
        # D21/§11.4: `redaction.history_scrub_file` (default `config/rules/secrets.txt`, as
        # `_write_config` provisions it) must actually reach `git-filter-repo` as
        # `--replace-text`, not just sit in `FleetConfig` unread — the exact gap D21 named.
        assert "--replace-text" in call.argv, call.argv
        scrub_path = Path(call.argv[call.argv.index("--replace-text") + 1])
        assert scrub_path == (fleet / "config" / "rules" / "secrets.txt").resolve(), scrub_path
        assert scrub_path.is_file(), scrub_path

    # No reduction to disclose: every repo in this fleet has an adapter, so the degraded path is
    # not merely unused here — it is unreported, which is the difference between a warning that
    # means something and one that fires on every run (§3.3 step 2 / Rule 11).
    body = payload(result)
    assert body["adapter_unavailable"] == [], body
    kinds = {row[0] for row in query(fleet, "SELECT kind FROM findings")}
    assert "EcosystemAdapterUnavailable" not in kinds, kinds


def test_a_degraded_repo_with_no_rhi_repo_exits_7(
    fleet: Path, monorepo: Path, bazel: FakeBazel, filter_repo: FakeFilterRepo  # noqa: F811
) -> None:
    """D93 / SPEC §3.5.1 point 5: a run with a `DEGRADED` repo and NO
    `REQUIRES_HUMAN_INTERVENTION` repo exits **7**, not 0 — the specific trigger D93 names,
    proved in isolation from the already-covered RHI trigger (`test_phase_four_withholds_a_repo_
    whose_phase_three_did_not_succeed` and others in this file cover RHI).

    A real stub-consumer scenario is proven elsewhere; this test isolates `cli.py::build`'s
    exit-code determination by writing the `phases` row directly, so the assertion is about what
    the CLI does with a `DEGRADED` phase-3 row, not about how a repo comes to hold one.
    """
    transformed(fleet)
    first = build(fleet, "--no-sandbox")
    assert first.exit_code == ExitCode.SUCCESS, first.output
    before = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 3"))
    assert "REQUIRES_HUMAN_INTERVENTION" not in before.values(), before
    victim = sorted(before)[0]

    conn = sqlite3.connect(fleet / "state" / "fleet.db", isolation_level=None)
    try:
        conn.execute(
            "UPDATE phases SET status = 'DEGRADED' WHERE repo_id = ? AND phase = 3",
            (victim,),
        )
    finally:
        conn.close()

    result = build(fleet, "--no-sandbox")
    assert result.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION == 7, result.output

    after = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 3"))
    assert after[victim] == "DEGRADED", after
    assert "REQUIRES_HUMAN_INTERVENTION" not in after.values(), after


def test_a_degraded_repo_at_phase_four_with_no_rhi_repo_exits_7(
    fleet: Path, monorepo: Path, bazel: FakeBazel  # noqa: F811
) -> None:
    """D93 / SPEC §3.5.1 point 5, Phase 4: the same trigger, at `cli.py::verify`'s own exit-code
    determination site — a genuinely separate call site from `build`'s (`_needs_human_attention`
    is shared, but each phase reads its own `phases` rows), so this is not redundant with the
    Phase 3 test above.
    """
    transformed(fleet)
    assert build(fleet, "--no-sandbox").exit_code == ExitCode.SUCCESS
    first = verify(fleet, "--rdeps-limit", "3")
    assert first.exit_code == ExitCode.SUCCESS, first.output
    before = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 4"))
    assert "REQUIRES_HUMAN_INTERVENTION" not in before.values(), before
    victim = sorted(before)[0]

    conn = sqlite3.connect(fleet / "state" / "fleet.db", isolation_level=None)
    try:
        conn.execute(
            "UPDATE phases SET status = 'DEGRADED' WHERE repo_id = ? AND phase = 4",
            (victim,),
        )
    finally:
        conn.close()

    result = verify(fleet, "--rdeps-limit", "3")
    assert result.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION == 7, result.output

    after = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 4"))
    assert after[victim] == "DEGRADED", after
    assert "REQUIRES_HUMAN_INTERVENTION" not in after.values(), after


def test_a_verify_provider_reaching_rhi_in_an_earlier_wave_blocks_its_later_wave_dependent(
    fleet: Path, monorepo: Path, bazel: FakeBazel  # noqa: F811
) -> None:
    """D125 (round VI task 78): does `_verify_impl`'s wave loop have the same cross-wave
    `blocked_by` propagation gap D123 measured for `_transform_impl` (ADR-0127)? Mirrors that
    test's own shape (`tests/test_transform_e2e.py::
    test_a_provider_failing_in_an_earlier_wave_blocks_its_later_wave_dependent_in_one_run`) as
    closely as `fleet verify`'s own PASS structure allows: `acme-lib-py` (wave 0) reaches
    `REQUIRES_HUMAN_INTERVENTION` through a REAL VERIFY dispatch (no hand-seeding -- a real
    `bazel build` failure via the injected `FakeBazel` seam, the same mechanism this file's
    `test_a_build_failure_is_structured_and_does_not_take_its_siblings_down` uses for Phase 3),
    and its direct dependent `acme-app-py` is scheduled into a LATER wave (wave 1,
    `tests/test_scan_e2e.py`'s own `waves["acme-lib-py"] < waves["acme-app-py"]` assertion, which
    holds identically at Phase 4 since `wave_members` is phase-independent) in the SAME `fleet
    verify` invocation.

    Phase 3 (`build()`) is driven to a clean `SUCCEEDED` for every repo FIRST, against the same
    `FakeBazel` instance with an empty `fail` table, so `_gated_members`'s predecessor=BUILD gate
    admits every repo into VERIFY's wave 0/1. Only AFTER that does the fixture inject the
    failure (`bazel.fail[("build", DESTINATIONS["acme-lib-py"])] = 34`) -- mutating the same
    `FakeBazel` instance rather than constructing a second one, since `cli.BAZEL_RUNNER` is bound
    once by the `bazel` fixture and the seam must stay the one instance both phases dispatch
    through.

    **Measured (round VI task 78, this xfail's own first red run):**
    `{'acme-lib-py': ('REQUIRES_HUMAN_INTERVENTION', []), 'acme-lib-ts': ('SUCCEEDED', []),
    'acme-app-py': ('SUCCEEDED', []), 'acme-app-ts': ('SUCCEEDED', [])}` -- CONFIRMED: the
    D123-shaped symptom, byte-for-byte the same wrong shape ADR-0127's own discovery fixture
    measured for TRANSFORM before its fix. See the docstring above's assertion for what SPEC
    §12.14 actually requires.
    """
    transformed(fleet)
    assert build(fleet, "--no-sandbox").exit_code == ExitCode.SUCCESS

    bazel.fail[("build", DESTINATIONS["acme-lib-py"])] = 34

    result = verify(fleet, "--rdeps-limit", "3")
    assert result.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION, result.output

    rows = query(fleet, "SELECT repo_id, status, blocked_by FROM phases WHERE phase = 4")
    statuses = {repo_id: (status, json.loads(blocked_by)) for repo_id, status, blocked_by in rows}

    assert statuses["acme-lib-py"][0] == "REQUIRES_HUMAN_INTERVENTION", statuses
    # §12.14's blast-containment clause requires this to read `("BLOCKED", ["acme-lib-py"])`.
    assert statuses["acme-app-py"] == ("BLOCKED", ["acme-lib-py"]), statuses
    for survivor in ("acme-lib-ts", "acme-app-ts"):
        assert statuses[survivor] == ("SUCCEEDED", []), statuses


def test_a_verify_provider_rhi_in_an_earlier_invocation_blocks_a_dependent_in_a_later_invocation(
    fleet: Path, monorepo: Path, bazel: FakeBazel  # noqa: F811
) -> None:
    """D126 / ADR-0130 (task-84): the VERIFY-side residual ADR-0129's own judgment call 3 flagged
    as real-but-out-of-scope — the SAME fixture the test above uses, driven across TWO SEPARATE
    `fleet verify --wave N` invocations rather than one invocation driving both waves. Mirrors
    `tests/test_transform_e2e.py::
    test_a_provider_rhi_in_an_earlier_invocation_blocks_a_dependent_in_a_later_invocation`
    as closely as `fleet verify`'s own gated PASS structure allows.

    Phase 3 (`build()`) is driven to a clean `SUCCEEDED` for every repo FIRST, so `_gated_members`'s
    predecessor=BUILD gate admits every repo into VERIFY's wave 0/1 in EITHER invocation. Only
    after that does the fixture inject the failure, exactly as the single-invocation test above
    does.

    **Before the fix** (old-fails/new-passes proof recorded in this task's report via the
    backup-file method, per CLAUDE.md Rule 12 — never `git stash`): `acme-lib-py` reaches
    `REQUIRES_HUMAN_INTERVENTION` for real inside the FIRST (`--wave 0`) `fleet verify` invocation
    — its one and only `propagate_blocked` call happens, and finishes, inside that process, before
    `acme-app-py`'s wave-1 VERIFY row exists anywhere. The SECOND (`--wave 1`) invocation's own
    pre-seed pass (`_gated_members`, gated on BUILD which already succeeded for every repo) creates
    `acme-app-py`'s row fresh, but nothing re-fires containment for the already-terminal,
    already-exited `acme-lib-py`. Measured byte-for-byte against `docs/INTEGRATION_HONESTY.md`'s
    D126 entry and research-46-report.md §1: `FIRST_EXIT=7, SECOND_EXIT=7,
    STATUSES={'acme-lib-py': ('REQUIRES_HUMAN_INTERVENTION', []), 'acme-lib-ts': ('SUCCEEDED', []),
    'acme-app-py': ('SUCCEEDED', []), 'acme-app-ts': ('SUCCEEDED', [])}` — `acme-app-py` reproduces
    D123/D125's exact original symptom across the invocation boundary.

    **After the fix**, `_repropagate_terminal_providers` runs at the start of the SECOND
    invocation, immediately after its own gated pre-seed pass and before wave dispatch: it finds
    `acme-lib-py` durably `REQUIRES_HUMAN_INTERVENTION` on record for `Phase.VERIFY` and re-invokes
    `propagate_blocked` against it, which now finds `acme-app-py`'s freshly pre-seeded row to write
    `BLOCKED` into. `acme-app-py` must read `BLOCKED` / `blocked_by == ['acme-lib-py']`.
    """
    transformed(fleet)
    assert build(fleet, "--no-sandbox").exit_code == ExitCode.SUCCESS

    bazel.fail[("build", DESTINATIONS["acme-lib-py"])] = 34

    first = verify(fleet, "--rdeps-limit", "3", "--wave", "0")
    assert first.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION, first.output

    second = verify(fleet, "--rdeps-limit", "3", "--wave", "1")
    assert second.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION, second.output

    rows = query(fleet, "SELECT repo_id, status, blocked_by FROM phases WHERE phase = 4")
    statuses = {repo_id: (status, json.loads(blocked_by)) for repo_id, status, blocked_by in rows}

    assert statuses["acme-lib-py"][0] == "REQUIRES_HUMAN_INTERVENTION", statuses
    assert statuses["acme-app-py"] == ("BLOCKED", ["acme-lib-py"]), statuses
    for survivor in ("acme-lib-ts", "acme-app-ts"):
        assert statuses[survivor] == ("SUCCEEDED", []), statuses


def test_the_build_runs_against_the_immutable_snapshot_and_nothing_else(
    fleet: Path, monorepo: Path, bazel: FakeBazel  # noqa: F811
) -> None:
    """The argv is exactly §3.3's, and the tree it ran in is the snapshot ref on its `attempts`
    row — a real `refs/fleet/<run_id>/integration/<seq>`, reachable from `integration`.

    Why this and not "a build ran": §3.3 step 1 says a 20-minute `bazel build` must not be able
    to see merges landing mid-build, so `BUILD_ERROR` is a property of a named tree rather than
    of scheduling luck. A build attributed to the moving branch name is unreproducible, and the
    only mechanical difference between the two is the string on this row.
    """
    transformed(fleet)
    assert build(fleet, "--no-sandbox").exit_code == ExitCode.SUCCESS

    dest = DESTINATIONS["acme-app-ts"]
    calls = bazel.for_dest("build", dest)
    assert len(calls) == 1, [c.argv for c in calls]
    # The cache flags carry the HOST paths here, and that is the whole of `--no-sandbox`: there is
    # no container, so `/cache/<name>` would name a directory that does not exist on this host and
    # bazel would either create it at the filesystem root or fail — either way the fleet's shared
    # caches would stay empty while every repo paid a cold fetch.
    assert calls[0].bazel == (
        "bazel",
        "build",
        f"//{dest}/...",
        "--keep_going",
        "--build_event_json_file=bazel-build-events.json",
        f"--disk_cache={(fleet / 'cache/bazel/disk').resolve()}",
        f"--repository_cache={(fleet / 'cache/bazel/repo').resolve()}",
    ), calls[0].bazel
    assert calls[0].cwd == build_worktree(fleet, "acme-app-ts"), calls[0].cwd
    assert not calls[0].containerised, "--no-sandbox must not wrap the command in a container"
    # `bazel test` follows `bazel build`, and both are recorded (§3.3's success criterion).
    assert bazel.for_dest("test", dest)[0].bazel[:3] == ("bazel", "test", f"//{dest}/...")

    rows = [row for row in attempts(fleet, 3) if row["repo_id"] == "acme-app-ts"]
    assert [row["command"][:2] for row in rows] == [["bazel", "build"], ["bazel", "test"]], rows
    refs = {row["integration_ref"] for row in rows}
    assert len(refs) == 1, refs
    ref = refs.pop()
    assert ref is not None and ref.startswith("refs/fleet/"), ref
    assert ref.rsplit("/", maxsplit=2)[-2] == "integration", ref
    sha = git(monorepo, "rev-parse", ref)
    assert len(sha) == 40, sha
    # Immutable AND on the branch: the snapshot names a tree `integration` actually reached.
    assert git(monorepo, "merge-base", "--is-ancestor", sha, "integration") == ""


def test_every_dispatched_worktree_carries_every_dest_the_root_files_were_computed_over(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    filter_repo: FakeFilterRepo,
) -> None:
    """**The general invariant behind the per-wave snapshot, stated over `dest`s and no ruleset.**

    `cli._fleet_support_files` computes the monorepo-ROOT files over **every plan prepared so
    far** — `//:Cargo.toml`'s `members`, `//:pnpm-workspace.yaml`'s importers, `//:.bazelignore`'s
    line per importer — and `cli._module_inputs` writes that same union into every dispatch's
    tree. So the root files have a *domain*: the set of destinations they describe. This asserts
    that every worktree Phase 3 dispatches contains that whole domain.

    **It was false before the snapshot moved, and false for every ecosystem at once.** Phase 3
    cut each repo's worktree from the snapshot taken at *its own* merge, so the first member of a
    wave held only its own merge while the root files already named every member's `dest`. Cargo
    is simply the ecosystem that says so out loud: `cargo metadata` fails the WHOLE workspace on
    a `members` entry whose directory is absent (`failed to load manifest for workspace member
    … No such file or directory`), so the first Rust repo of a wave failed 3/3 attempts and only
    the last one built. `npm_translate_lock` tolerates a pnpm importer directory that is not
    there and a requirements file names distributions rather than paths — which is why JS and
    Python were green over trees that were false about themselves in exactly the same way.

    **Why it is written like this and not as "the Rust build passes".** It needs no Bazel, no
    network and no package manager: it is a set comparison between what the root files were
    computed over and what the tree holds, so it cannot go green by changing *which* repo loses,
    and it stays true if an ecosystem's tolerance for a missing sibling ever changes. The
    destinations are read off `git-filter-repo`'s own `--path-rename` argv rather than restated,
    so a fixture that grows a repo is covered without touching this test.

    **The domain is bounded by the wave, and that is the claim — not more.** A repo's worktree is
    cut after its own wave's last ingest, so it owes every `dest` of its wave and of every earlier
    one; a LATER wave's repo is not in it, and no root file that repo's plan carries was computed
    over that later `dest` either. Two Rust repos in *different* waves are the residue this does
    not cover: the earlier wave settles, is never re-admitted, and its published root files stay
    behind. The two Rust fixtures land in ONE wave (asserted below, so the intra-wave case is
    really the one exercised) because neither declares an internal dependency.
    """
    add_repos(fleet, ["acme-codec-rs", "acme-case-rs"])
    transformed(fleet)
    result = build(fleet, "--no-sandbox")
    assert result.exit_code == ExitCode.SUCCESS, result.output

    dests = relocations(filter_repo)
    assert {"acme-codec-rs", "acme-case-rs"} <= set(dests), dests
    waves = {repo_id: wave_index(fleet, repo_id) for repo_id in dests}
    assert waves["acme-codec-rs"] == waves["acme-case-rs"], (
        "both Rust repos must share a wave for this to exercise the INTRA-wave case at all; "
        f"waves={waves}"
    )
    assert max(len([r for r in dests if waves[r] == w]) for w in set(waves.values())) > 1, (
        "every wave has one member, so no worktree owes a sibling's dest and this asserts nothing"
    )

    for repo_id, wave in waves.items():
        tree = build_worktree(fleet, repo_id)
        domain = sorted(dest for other, dest in dests.items() if waves[other] <= wave)
        missing = [dest for dest in domain if not (tree / dest).is_dir()]
        assert not missing, (
            f"{repo_id} built in a worktree with no {missing}, while the fleet-wide root files "
            f"in that same tree were computed over {domain}: the root files describe units the "
            f"tree does not have"
        )


def published_root_files(monorepo: Path) -> dict[str, str]:
    """Every BLOB at the monorepo root on `integration`, path → content.

    Read off `git ls-tree` rather than from a hard-coded list, so a fleet that grows an ecosystem
    grows this set without the test being edited — and so a root file that DISAPPEARS from the
    branch is as visible as one whose bytes changed.
    """
    out: dict[str, str] = {}
    for line in git(monorepo, "ls-tree", "integration").splitlines():
        meta, _, name = line.partition("\t")
        if meta.split()[1] == "blob":
            out[name] = git(monorepo, "show", f"integration:{name}")
    return out


def published_packages(monorepo: Path) -> set[str]:
    """Every generated `<dest>/BUILD.bazel` on `integration` — the packages that really PUBLISHED.

    The growth measure this file uses across invocation boundaries, and deliberately not the set
    of destinations history was rewritten onto: since the ingest pass covers the whole eligible
    fleet before the first build (ADR-0055), every repo's *history* is on the branch after the
    first invocation whatever `--wave` said. What a later invocation adds is a repo that has now
    BUILT and published its package, which is the thing "the branch grew" was always a proxy for.
    """
    return {
        path
        for path in git(monorepo, "ls-tree", "-r", "--name-only", "integration").splitlines()
        if path.endswith("/BUILD.bazel")
    }


#: A lockfile in the shape Bazel 9 writes, keyed by the registry a containerised build contacts.
#: Borrowed from `test_bazel.py` rather than re-spelled so that the bytes this file publishes and
#: the bytes the registry guard is tested against are one fixture.
A_BCR_KEYED_LOCK: str = _a_lockfile(BCR_DEFAULT_REGISTRY)


class LockWritingBazel(FakeBazel):
    """`FakeBazel`, plus the one effect real Bazel has on the tree it is pointed at.

    Real `bazel build` writes `MODULE.bazel.lock` into the workspace root as a side effect of
    resolving the module graph, and that side effect — not any output the harness parses — is the
    whole source of the bytes Phase 3 has to publish. `FakeBazel` answers from a table without
    touching the tree, which is exactly why the missing lockfile survived every existing test in
    this file: there was never a lockfile for the publish path to fail to carry.

    Only `build`/`test` write, and only into the `cwd` the seam was given, because that is what
    real Bazel does: a `query` in the Phase 4 verify worktree resolves the graph from its own
    output base and leaves that tree alone.
    """

    def __init__(self, log_root: Path, *, lock_text: str = A_BCR_KEYED_LOCK) -> None:
        super().__init__(log_root)
        self.lock_text = lock_text

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        deadline: float | None = None,
        timeout_s: float | None = None,
    ) -> ProcResult:
        result = await super().__call__(
            argv, cwd=cwd, env=env, deadline=deadline, timeout_s=timeout_s
        )
        command = tuple(argv)
        if cwd is not None and "bazel" in command:
            unwrapped = command[command.index("bazel") :]
            if len(unwrapped) > 1 and unwrapped[1] in {"build", "test"}:
                (cwd / MODULE_LOCK_PATH).write_text(self.lock_text, encoding="utf-8")
        return result


def test_the_lockfile_bazel_wrote_in_the_build_worktree_is_published_on_integration(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    filter_repo: FakeFilterRepo,
    resolver: FakeResolver,
    gazelle: FakeGazelle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**`MODULE.bazel.lock` reaches the branch, carried as planned bytes.**

    Why it has to: a `--network=none` container with a fully warmed repository cache and no
    lockfile exits **32** before analysis — `Error computing the main repository mapping: Error
    accessing registry https://bcr.bazel.build/` — because without `registryFileHashes` Bazel has
    to re-fetch the registry metadata for every module in the graph. The same container with the
    matching lockfile exits 0. So a generated monorepo that omits this file is one nobody can
    build offline, and until now nothing in the pipeline carried it: the file is written by Bazel
    during the VERIFY unit and every existing seam left the tree untouched.

    The mechanism is ADR-0056's, not a new one: bytes a tool produced are captured from the tree
    the tool wrote them in, wrapped in a `SupportFile`, and written back by `materialize` — the
    one writer that turns planned bytes into files — so the published bytes are asserted here to
    be *exactly* what the tool wrote and not a re-read of a tree a later step may have dirtied.

    **Exactly one recording commit for four dispatches** is the second half, and it is what makes
    the publish idempotent rather than merely correct. Every dispatch of this fleet writes the
    same lock, and `_publish_module_lock` compares bytes before it writes, so three of the four
    find the branch already carrying their resolution and mint nothing. A publish that committed
    unconditionally would put a commit per repo per run on `integration` forever.
    """
    monkeypatch.setattr(cli, "BAZEL_RUNNER", LockWritingBazel(fleet / "artifacts" / "fake-bazel"))
    _ = (filter_repo, resolver, gazelle)
    transformed(fleet)

    result = build(fleet, "--no-sandbox")

    assert result.exit_code == ExitCode.SUCCESS, result.output
    published = published_root_files(monorepo)
    assert published.get(MODULE_LOCK_PATH) == A_BCR_KEYED_LOCK, sorted(published)
    # The published lock is keyed by the registry the container's Bazel will contact. Asserted
    # over the BRANCH's bytes, so the guard is a statement about what shipped.
    check_lock_registry(published[MODULE_LOCK_PATH], registry=BCR_DEFAULT_REGISTRY)
    recordings = [
        line
        for line in git(monorepo, "log", "--format=%s", "integration").splitlines()
        if line.startswith(f"Record {MODULE_LOCK_PATH}")
    ]
    assert len(recordings) == 1, recordings
    # ...and nothing complained, which is what makes the two tests below mean something: the
    # guard wired into the publish path is silent on a lock that matches.
    assert "module_lock_foreign_registry" not in result.output, result.output[-4000:]
    assert lock_registry_findings(fleet) == [], lock_registry_findings(fleet)


def lock_registry_findings(root: Path) -> list[tuple[str, str]]:
    """`(repo_id, detail)` for every `ModuleLockForeignRegistry` row, sorted.

    Read from `findings` rather than from the run's stdout because the log line dies with the
    process: the whole reason the worker carries `module_lock_foreign_registry` on its output is
    so that the run's own state remembers that the branch's lockfile cannot be shown to work
    offline, and a test that only grepped the console would leave the durable half unproven.
    """
    rows = query(
        root,
        "SELECT repo_id, payload FROM findings WHERE kind = 'ModuleLockForeignRegistry' "
        "ORDER BY repo_id",
    )
    return [(str(row[0]), str(json.loads(str(row[1]))["detail"])) for row in rows]


def test_a_mirror_keyed_lockfile_is_published_and_recorded_rather_than_shipped_silently(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    filter_repo: FakeFilterRepo,
    resolver: FakeResolver,
    gazelle: FakeGazelle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**`check_lock_registry` runs in PRODUCTION now, over the bytes that reach the branch.**

    It was written as a guard and then called from tests only — `tests/test_bazel.py` and one
    assertion in the test above — so nothing in `src/` had ever asked the question about a lock a
    real publish committed. This drives the whole chain: `LockWritingBazel` writes a
    `registryFileHashes` map keyed by `BCR_MIRROR_REGISTRY`, exactly as a real Bazel run against
    that mirror would, and the assertions are made on the branch and on `findings`.

    **Why the outcome is "published, and recorded" rather than "refused".** A mirror-keyed lock
    is not wrong here — it is what a host whose `bcr.bazel.build` is blackholed produces, which
    is the case `BCR_MIRROR_REGISTRY` exists for and the case this repository's own
    `bazel_registry` fixture falls back to. Worse, the check cannot be certain it is right: the
    registry it compares against comes from `build.registry`, and a `common --registry=` line in
    the monorepo's committed `.bazelrc` overrides that without the harness ever seeing it. So a
    refusal would withhold a correct artifact from the offline builds it exists to serve. What is
    NOT acceptable is the branch quietly carrying a lock that makes the tree LOOK offline-ready
    while a `--network=none` container still dies at `Error computing the main repository
    mapping` with exit 32 — the identical failure to shipping no lock at all.

    So three things are asserted, and the third is the one the guard was commissioned for: the
    lock is on the branch byte-for-byte; the run said so on the console; and the run's own state
    carries a `findings` row per publishing repo naming the foreign host, so the disclosure
    outlives the process that made it.
    """
    monkeypatch.setattr(
        cli,
        "BAZEL_RUNNER",
        LockWritingBazel(
            fleet / "artifacts" / "fake-bazel", lock_text=_a_lockfile(BCR_MIRROR_REGISTRY)
        ),
    )
    _ = (filter_repo, resolver, gazelle)
    transformed(fleet)

    result = build(fleet, "--no-sandbox")

    # 1. published, not refused — and the repos still SUCCEEDED, because a lock that cannot be
    #    shown to work offline is a disclosure and not a build failure.
    assert result.exit_code == ExitCode.SUCCESS, result.output
    published = published_root_files(monorepo)
    assert published.get(MODULE_LOCK_PATH) == _a_lockfile(BCR_MIRROR_REGISTRY), sorted(published)
    statuses = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 3"))
    assert statuses == dict.fromkeys(DESTINATIONS, "SUCCEEDED"), statuses

    # 2. ...and said so, where an operator watching the run sees it.
    assert "module_lock_foreign_registry" in result.output, (
        "a lockfile keyed by a registry the container's Bazel never contacts reached the branch "
        "and the run said nothing, so a monorepo that cannot build offline is indistinguishable "
        f"from one that can:\n{result.output[-4000:]}"
    )

    # 3. ...and the state remembers it. This is the half a log line cannot carry.
    findings = lock_registry_findings(fleet)
    assert [repo for repo, _ in findings] == sorted(DESTINATIONS), (
        f"expected one ModuleLockForeignRegistry row per publishing repo, got {findings}"
    )
    for repo, detail in findings:
        assert urlsplit(BCR_MIRROR_REGISTRY).netloc in detail, (repo, detail)
        assert BCR_DEFAULT_REGISTRY in detail, (
            f"the finding for {repo} does not name the registry the lock was expected to be keyed "
            f"by, so it does not tell an operator what to re-resolve against: {detail}"
        )


def test_a_lock_keyed_by_the_configured_registry_draws_no_complaint(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    filter_repo: FakeFilterRepo,
    resolver: FakeResolver,
    gazelle: FakeGazelle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same mirror-keyed lock, on a fleet whose `build.registry` IS the mirror: no complaint.

    The pair to the test above, and it is what stops the wiring from being a check that fires on
    any lock a mirror ever touched. `BCR_MIRROR_REGISTRY` is a supported operator setting — §9's
    `build.registry`, which exists because `bcr.bazel.build` is blackholed on some networks — so
    on such a fleet a mirror-keyed lock is the CORRECT lock and warning about it would be noise
    that trains an operator to ignore the row.

    It is also the only assertion that can tell "the registry came from config" from "the
    registry is `BCR_DEFAULT_REGISTRY` spelled into the publish path". The two are indis-
    tinguishable on a default fleet and differ only here: the bytes are identical to the test
    above and the config is the single variable, so a hardcoded expectation fails this test and
    nothing else in the suite.
    """
    config = fleet / "config" / "fleet.yaml"
    config.write_text(
        f"{config.read_text(encoding='utf-8')}build:\n  registry: {BCR_MIRROR_REGISTRY}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli,
        "BAZEL_RUNNER",
        LockWritingBazel(
            fleet / "artifacts" / "fake-bazel", lock_text=_a_lockfile(BCR_MIRROR_REGISTRY)
        ),
    )
    _ = (filter_repo, resolver, gazelle)
    transformed(fleet)

    result = build(fleet, "--no-sandbox")

    assert result.exit_code == ExitCode.SUCCESS, result.output
    published = published_root_files(monorepo)
    assert published.get(MODULE_LOCK_PATH) == _a_lockfile(BCR_MIRROR_REGISTRY), sorted(published)
    assert "module_lock_foreign_registry" not in result.output, (
        "the fleet's configured registry IS this mirror, so the published lock is keyed exactly "
        f"as the Bazel that reads it will ask — the warning is a false alarm:\n"
        f"{result.output[-4000:]}"
    )
    assert lock_registry_findings(fleet) == [], lock_registry_findings(fleet)


def test_a_build_whose_bazel_wrote_no_lockfile_publishes_none_rather_than_an_empty_one(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    filter_repo: FakeFilterRepo,
) -> None:
    """**The bootstrap, asserted: no lockfile in the worktree publishes no lockfile — loudly.**

    On the very first run there is no `MODULE.bazel.lock` anywhere, because the Bazel that writes
    one is the Bazel this dispatch is about to run; a dispatch that re-enters owing only the
    publish unit never runs one at all. The harness has three options and two of them are worse
    than the state they fix:

    * **fail the publish** — costs this repo the `BUILD.bazel`/`MODULE.bazel` it legitimately
      generated over an artifact the *next* build produces;
    * **publish an empty one** — the `Resolution` docstring's defect at monorepo scale: a lock
      claiming a resolution nobody computed, which Bazel either overwrites (so it bought nothing)
      or, under `--lockfile_mode=error`, refuses. Either way the tree LOOKS offline-ready while
      the container still exits 32;
    * **publish nothing and say so** — what this asserts.

    "Says so" is the load-bearing half. A tree that silently ships without the file is
    indistinguishable from one that ships with it until somebody runs an offline build, so the
    absence is logged at WARNING naming the consequence. `FakeBazel` writes no lock, which makes
    this test the *ordinary* configuration of every other test in this file — the behaviour being
    pinned is therefore also the behaviour those tests have been exercising all along.
    """
    _ = filter_repo
    transformed(fleet)

    result = build(fleet, "--no-sandbox")

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert bazel.commands("build"), "no build ran, so the absence below proves nothing"
    published = published_root_files(monorepo)
    assert published, "nothing at all was published, so the absence below proves nothing"
    assert MODULE_LOCK_PATH not in published, (
        f"a {MODULE_LOCK_PATH} reached the branch although no Bazel ever wrote one — a "
        f"synthesized lock is a resolution nobody performed: {published[MODULE_LOCK_PATH][:200]}"
        if MODULE_LOCK_PATH in published
        else ""
    )
    assert "module_lock_absent" in result.output, (
        "the tree shipped with no MODULE.bazel.lock and the run said nothing about it, so a "
        "monorepo no container can build offline is indistinguishable from one that can:\n"
        f"{result.output[-4000:]}"
    )


def test_a_second_invocation_republishes_root_files_that_still_describe_the_whole_branch(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    filter_repo: FakeFilterRepo,
) -> None:
    """**The fleet-wide root files on `integration` never describe FEWER repos than the branch
    holds** — whatever the invocation boundaries were.

    §3.3 step 2's root files are fleet-wide by construction: ONE `MODULE.bazel`, ONE
    `pnpm-workspace.yaml`, ONE `Cargo.toml`, ONE `.bazelignore` at the monorepo root, each
    describing every unit in the monorepo. The branch only ever GROWS — a wave merges repos in
    and nothing removes them — so any republication of those files may add to them and may never
    take away. That is the whole claim here, and it is stated over the published bytes rather
    than over any ecosystem's syntax: no line that the branch carried before may be missing
    after, and no root file may vanish.

    **Why the invocation is split rather than run once.** Within a single `fleet build` the
    invariant held even before ADR-0055, because `plans` accumulates across the waves that
    invocation drives — which is exactly why every existing test here was green while this one
    was `xfail(strict=True)`. The interesting operator flow is the one that crosses a process
    boundary: driving wave 0, then coming back for the rest. That is not an exotic path.
    `fleet build --wave N` is a shipped flag with a per-wave cost ceiling behind it
    (`_check_wave_budget`), a wave that halts leaves the later waves open by design, and
    `fleet resume` is still `_unavailable` — so re-invoking `fleet build` is the ONLY way an
    operator continues a partially complete run today.

    **What used to happen, and what closed it.** `cli._build_impl`'s `plans` is process-local and
    was never rehydrated from SQLite, while `cli._open_phase_waves` drops every wave whose members
    are all settled — so the second invocation computed `_fleet_support_files` and
    `_module_inputs` over the UNSETTLED wave's repos alone. Measured: `MODULE.bazel` lost
    `rules_jvm_external` and `rules_rust` with their `single_version_override`s, the whole
    `maven.install` block and the whole `crate.from_cargo`/`rust.toolchain` block;
    `//:pnpm-workspace.yaml` and `//:.bazelignore` lost the wave-0 JS importers; the root
    `BUILD.bazel` stopped exporting `Cargo.toml`/`Cargo.lock`. Both invocations exited 0, and
    `_check_root_file_domain` could not fire because it read its domain off the same shrunken
    `plans` and a shrunken domain is trivially contained. ADR-0055 hoists the ingest pass out of
    the wave loop and derives the domain from SQLite instead, so the root files are computed over
    one fixed, maximal unit set for the whole run.

    The fixture is deliberately polyglot and deliberately split across the wave boundary: Java
    and Rust land in wave 0 and nothing in wave 1 is Java or Rust, so a root file computed over
    wave 1 alone cannot mention them; and two extra JS repos land in wave 0 so the JS root files
    have importers to lose while JS is still present in wave 1. Both halves are asserted below
    before the invariant is, because a fixture that collapsed into one wave would make this test
    pass while proving nothing — as would a second invocation that re-drove the settled wave, so
    that is asserted too.
    """
    add_repos(fleet, ["acme-commons-java", "acme-codec-rs", "acme-case-rs",
                      "acme-ui-ts", "acme-report-ts"])
    transformed(fleet)
    waves = {
        str(row[0]): int(row[1])
        for row in query(
            fleet,
            "SELECT node_id, wave_index FROM wave_members WHERE node_kind = 'REPO'",
        )
    }
    assert len(set(waves.values())) > 1, f"one wave only — nothing crosses an invocation: {waves}"
    first = sorted(set(waves.values()))[0]

    assert build(fleet, "--no-sandbox", "--wave", str(first)).exit_code == ExitCode.SUCCESS
    before = published_root_files(monorepo)
    packages_before = published_packages(monorepo)
    assert before, "the first invocation published no root file at all"
    settled = sorted(
        dest for repo, dest in relocations(filter_repo).items() if waves[repo] == first
    )
    assert settled, f"no repo landed in wave {first}, so nothing can be dropped: {waves}"

    rest = build(fleet, "--no-sandbox")
    assert rest.exit_code == ExitCode.SUCCESS, rest.output
    driven = payload(rest)["waves"]
    assert driven != [], (
        "the second invocation drove no wave, so nothing was republished and this asserts "
        f"nothing: {payload(rest)}"
    )
    # The settled wave is NOT re-driven — which is the whole hazard. Its units are on the branch
    # and its repos are `SUCCEEDED`, so any domain read off "what this invocation dispatched"
    # excludes them, and the root files it republishes would stop describing them.
    assert first not in driven, (
        f"the second invocation re-drove wave {first}, so its units are in whatever this "
        f"invocation dispatched and a shrunken domain could not be observed: {driven}"
    )
    after = published_root_files(monorepo)
    packages_after = published_packages(monorepo)
    assert packages_before < packages_after, (
        "the branch must have GROWN across the two invocations for the monotonicity claim below "
        f"to mean anything: {sorted(packages_before)} -> {sorted(packages_after)}"
    )
    republished = {path.rsplit("/", 1)[0] for path in packages_after - packages_before}
    assert not (set(settled) & republished), (
        f"wave {first}'s packages were republished by the second invocation, so this would pass "
        f"on a domain that had merely been recomputed rather than kept: {settled}"
    )

    vanished = sorted(set(before) - set(after))
    assert not vanished, f"root file(s) {vanished} were on the branch and are not any more"
    lost = {
        path: [line for line in before[path].splitlines() if line.strip()
               and line not in after[path].splitlines()]
        for path in sorted(before)
    }
    lost = {path: lines for path, lines in lost.items() if lines}
    assert not lost, (
        "the second `fleet build` republished fleet-wide root files that no longer describe "
        "every repo on the branch — these lines were published by the first invocation and are "
        f"gone from the branch tip while every repo they name is still merged into it: {lost}"
    )


def test_repo_and_wave_narrow_dispatch_and_never_the_root_file_domain(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    filter_repo: FakeFilterRepo,
) -> None:
    """`--repo` chooses what is BUILT. It does not choose what the root files describe.

    The flags are the second door into the shrink: if the fleet-wide root files were computed
    over "the repos this invocation dispatched", then `fleet build --repo acme-codec-rs` would
    publish a `MODULE.bazel`, a `pnpm-workspace.yaml` and a `.bazelignore` describing one repo,
    and the next full invocation would have to put them back. The domain is derived from
    `wave_members` ⋈ `phases` instead (`cli._eligible_build_units`), which no flag reaches.

    Asserted as an EQUALITY between two invocations rather than by naming any ecosystem's syntax:
    the narrowed run publishes byte-identical root files to the full run that follows it. That is
    the strongest available form of "narrowing dispatch did not narrow the domain" — it holds for
    every root file every adapter declares, including ones this fixture does not have — and it is
    only checkable because §11.6 makes those bytes a function of the plan and ADR-0054 makes the
    published bytes the planned ones.

    The three halves that keep it from passing vacuously are asserted first: dispatch really was
    narrowed to one repo, the fleet really is bigger than that repo, and the narrowed run really
    did publish root files.
    """
    add_repos(fleet, ["acme-commons-java", "acme-codec-rs", "acme-ui-ts"])
    transformed(fleet)

    scoped = build(fleet, "--no-sandbox", "--repo", "acme-codec-rs")
    assert scoped.exit_code == ExitCode.SUCCESS, scoped.output
    dispatched = {row["repo_id"] for row in attempts(fleet, 3)}
    assert dispatched == {"acme-codec-rs"}, f"--repo did not narrow dispatch: {dispatched}"
    merged = relocations(filter_repo)
    assert len(merged) > 1, f"the fleet must be bigger than the one dispatched repo: {merged}"

    narrowed = published_root_files(monorepo)
    assert narrowed, "the narrowed invocation published no root file, so this asserts nothing"
    # Every unit of the whole fleet is in the tree the one dispatched repo built in — which is
    # what the root files in that same tree claim, and what `_check_root_file_domain` enforces.
    tree = build_worktree(fleet, "acme-codec-rs")
    absent = sorted(dest for dest in merged.values() if not (tree / dest).is_dir())
    assert not absent, f"acme-codec-rs built in a worktree with no {absent}"

    full = build(fleet, "--no-sandbox")
    assert full.exit_code == ExitCode.SUCCESS, full.output
    assert payload(full)["waves"] != [], "the full run drove nothing, so it republished nothing"
    complete = published_root_files(monorepo)
    differing = sorted(
        path
        for path in set(narrowed) | set(complete)
        if narrowed.get(path) != complete.get(path)
    )
    assert complete == narrowed, (
        "the root files a `--repo`-scoped `fleet build` published are not the ones the full "
        "fleet publishes, so the flag narrowed the domain and not only the dispatch: "
        f"{differing}"
    )


def test_a_repo_that_cannot_be_ingested_leaves_the_domain_instead_of_haunting_it(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    filter_repo: FakeFilterRepo,
) -> None:
    """A repo whose §3.3 step 1 fails is dropped from the DOMAIN, not merely from the dispatch.

    The hazard the hoisted ingest creates and has to answer. The domain is derived from SQLite
    *before* anything is ingested, so a repo that then fails to merge would leave its `dest` in
    the fleet-wide root files — a `//:Cargo.toml` `members` entry or a `pnpm-workspace.yaml`
    importer naming a directory that no worktree will ever contain, which is defect D15 exactly
    and which `_check_root_file_domain` would then (correctly, loudly) fail the whole run over.

    It is safe to drop precisely because the ingest pass runs before the first build: nothing has
    been dispatched and nothing has been published, so the domain is still fixed and still maximal
    for every wave that follows. That is NOT true of a failure discovered later, which is why a
    re-plan failure at a later wave keeps its unit in the domain instead.

    The failure is induced by removing the repo's Phase 2 worktree, which is the input §3.3 step 1
    names — the same `BuildStepUnavailableError` an operator gets from a fleet whose transform was
    never run for one repo.
    """
    add_repos(fleet, ["acme-ui-ts"])
    transformed(fleet)
    shutil.rmtree(fleet / "work" / "acme-ui-ts")

    result = build(fleet, "--no-sandbox")
    assert result.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION, result.output
    statuses = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 3"))
    assert statuses["acme-ui-ts"] == "REQUIRES_HUMAN_INTERVENTION", statuses
    assert {
        repo: status for repo, status in statuses.items() if repo != "acme-ui-ts"
    } == dict.fromkeys(DESTINATIONS, "SUCCEEDED"), statuses
    kinds = dict(query(fleet, "SELECT repo_id, kind FROM findings WHERE repo_id = 'acme-ui-ts'"))
    assert kinds == {"acme-ui-ts": "BuildPreparationFailed"}, kinds

    # The run finished: no `RootFileDomainError` over a `dest` that never materialized.
    published = published_root_files(monorepo)
    assert published, "nothing was published, so the absence below proves nothing"
    haunted = sorted(path for path, text in published.items() if "ts/acme/ui" in text)
    assert not haunted, (
        f"root file(s) {haunted} name `ts/acme/ui`, whose repo never merged: the fleet-wide root "
        "files describe a unit no worktree contains"
    )
    # ... and the sibling of the same ecosystem, which DID merge, is still described.
    assert any("ts/acme/lib" in text for text in published.values()), published


class ReplanRefusal:
    """`cli.PLAN_BUILD_HOOK`, armed to refuse ONE repo's **second** planning and nothing else.

    The seam is deliberately ignorant of which of `_build_impl`'s passes is calling it, so the
    "which call is the re-plan" decision lives here, in the test, where it can be *asserted*
    rather than assumed. It is the second call by construction: PASS 2 plans every ingested unit
    exactly once from the run's first snapshot, and the wave loop re-plans a member exactly once
    more when an earlier wave has published. `calls` is kept so both halves of that sentence are
    checked instead of trusted.

    `published_at_refusal` is the snapshot of the monorepo root taken **at the moment the refusal
    is raised**. It is what makes this a POST-domain failure rather than a differently-spelled
    pre-domain one: ADR-0055 keeps the unit in the domain precisely because root files naming its
    `dest` are already on the branch, and a test that never looked would pass just as well
    against a run where nothing had published yet.
    """

    def __init__(self, monorepo: Path, repo_id: str, *, detail: str) -> None:
        self.monorepo = monorepo
        self.repo_id = repo_id
        self.detail = detail
        self.calls: list[str] = []
        self.published_at_refusal: dict[str, str] = {}

    def __call__(self, repo_id: str) -> None:
        self.calls.append(repo_id)
        if repo_id == self.repo_id and self.calls.count(repo_id) == 2:
            self.published_at_refusal = published_root_files(self.monorepo)
            raise cli.BuildStepUnavailableError(self.detail)


class DomainSpy:
    """Every `_check_root_file_domain` call of one run, recorded and then DELEGATED.

    The three things ADR-0055's post-domain class is a claim about — the run's `domain`, the
    `plans` the root files were computed over, and the `dispatched` subset the containment half
    is run against — are process-local, and the published bytes cannot distinguish "kept" from
    "dropped from both at once": ADR-0055 computes the root files ONCE in PASS 3, before the wave
    loop, so nothing a later wave does to `plans` can change what any repo republishes. Watching
    the guard's own arguments is therefore the only way to ask the machine what the code comment
    asserts, and it is a pure observer: the real function still runs, with the real arguments, and
    still raises what it would have raised.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], tuple[str, ...], dict[str, str]]] = []
        self._inner = cli._check_root_file_domain

    async def __call__(
        self,
        plans: Mapping[str, cli._BuildPlan],
        dispatched: Sequence[str],
        *,
        domain: Mapping[str, str],
    ) -> None:
        self.calls.append((tuple(sorted(plans)), tuple(dispatched), dict(domain)))
        await self._inner(plans, dispatched, domain=domain)


def _refuse_a_later_waves_replan(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ReplanRefusal, str, list[str]]:
    """Drive `transform`, then arm the seam against one member of the fleet's LAST wave.

    Returns the armed hook, the repo it will refuse, and that repo's wave-mates. Split out
    because the two tests below make two different claims about one run and neither is worth
    weakening to share an assertion with the other.
    """
    transformed(fleet)
    waves = {
        str(row[0]): int(row[1])
        for row in query(
            fleet, "SELECT node_id, wave_index FROM wave_members WHERE node_kind = 'REPO'"
        )
    }
    assert len(set(waves.values())) > 1, (
        f"one wave only, so no wave can publish before another is planned and the POST-domain "
        f"branch is unreachable: {waves}"
    )
    last = max(waves.values())
    members = sorted(repo for repo, index in waves.items() if index == last)
    assert len(members) > 1, (
        f"wave {last} has a single member, so 'the rest of the fleet still builds' could not be "
        f"asserted against a repo in the SAME wave as the failure: {waves}"
    )
    # WHICH member is refused is not arbitrary, and picking the first one alphabetically made
    # this test vacuous once already. The claim the two failure classes turn on is "earlier waves
    # already published root files NAMING this dest", and only some ecosystems' root files name
    # destinations at all: `//:pnpm-workspace.yaml` and `//:.bazelignore` carry a line per JS
    # `dest`, while Python's `//:requirements.lock` is a resolved dependency list that mentions no
    # repo of this fleet. Refusing the Python member would leave nothing on the branch for a pop
    # to damage, so the interesting branch would be exercised against an uninteresting unit.
    npm = [repo for repo in members if repo_ecosystem(fleet, repo) is Ecosystem.NPM]
    assert npm, (
        f"no member of wave {last} has an ecosystem whose fleet-wide root files name a `dest`, so "
        f"'the branch already names it' cannot be asserted at the moment of the failure: {members}"
    )
    victim = npm[0]
    siblings = [repo for repo in members if repo != victim]
    hook = ReplanRefusal(
        monorepo, victim, detail=f"the worktree for {victim} could not be cut for this wave"
    )
    monkeypatch.setattr(cli, "PLAN_BUILD_HOOK", hook)
    return hook, victim, siblings


def test_a_replan_failure_after_a_wave_published_keeps_its_unit_in_the_domain(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    filter_repo: FakeFilterRepo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0055's **post-domain** failure class, which had no test: the unit STAYS in the domain.

    The mirror image of `test_a_repo_that_cannot_be_ingested_leaves_the_domain_instead_of_haunting
    _it` above, and the two must disagree or one of them is wrong. A PASS 1/2/3 failure pops the
    repo's `dest` out of the run's domain, which is safe because nothing has been dispatched and
    nothing published. A **re-plan failure at a later wave** is the opposite case in every
    respect: an earlier wave has already published fleet-wide root files that NAME this `dest`,
    and the repo's history is already on the integration branch. Popping it there would republish
    root files describing fewer repos than the branch holds — the exact defect ADR-0055 exists to
    close — so the code keeps it in `domain` and in `plans`, marks the repo
    `REQUIRES_HUMAN_INTERVENTION` so the scheduler will not lease it, and excludes it from
    `_check_root_file_domain`'s containment half because its worktree is the thing that could not
    be made.

    That behaviour was recorded in a code comment and in ADR-0055 and never asserted, because it
    is unreachable from any fixture: every way of breaking a repo's planning also breaks its PASS
    2 planning, where the pre-domain branch drops it and the interesting branch is never entered.
    `cli.PLAN_BUILD_HOOK` is the seam that closes that gap, and it is inert unless a test sets it.

    **The claims, and the negative one is the point.** The repo is marked and recorded; root
    files naming its `dest` were already on the branch at the moment the refusal was raised
    (asserted there, not after the fact — that is the entire justification for the two classes
    differing); the published root files STILL name it afterwards; and the guard the later wave
    actually ran was handed a `domain` and a `plans` that BOTH still contain the failed unit,
    with only `dispatched` narrowed. That last one is watched at the guard rather than inferred
    from the branch, because ADR-0055 resolves the root files once in PASS 3 and a mid-run drop
    of `plans` *and* `domain` together would leave every published byte identical — the pop the
    pre-domain path performs is only observable from inside.
    """
    hook, victim, siblings = _refuse_a_later_waves_replan(fleet, monorepo, monkeypatch)
    dest = DESTINATIONS[victim]
    spy = DomainSpy()
    monkeypatch.setattr(cli, "_check_root_file_domain", spy)

    result = build(fleet, "--no-sandbox")
    assert result.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION, result.output

    # The seam fired where the test claims it did: twice for the victim — once in PASS 2, where
    # it succeeded, and once at the later wave, where it refused. One call would mean the wave
    # loop never re-planned and this test is asserting the pre-domain path under a new name.
    assert hook.calls.count(victim) == 2, (
        f"{victim} was planned {hook.calls.count(victim)} time(s); the POST-domain branch is only "
        f"reached by the wave loop's RE-plan, so anything but 2 means it was never taken: "
        f"{hook.calls}"
    )
    # ... and a wave really had published before it refused.
    assert hook.published_at_refusal, (
        "nothing was published at the moment the re-plan failed, so this run never entered the "
        "post-publication state the two failure classes differ over"
    )
    naming = sorted(
        path for path, text in hook.published_at_refusal.items() if dest in text
    )
    assert naming, (
        f"no root file on the branch named `{dest}` when its re-plan failed, so dropping it would "
        f"have been harmless and this test proves nothing: "
        f"{sorted(hook.published_at_refusal)}"
    )

    # 1. marked, and recorded as a preparation failure like its pre-domain sibling.
    statuses = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 3"))
    assert statuses[victim] == "REQUIRES_HUMAN_INTERVENTION", statuses
    kinds = dict(query(fleet, "SELECT repo_id, kind FROM findings WHERE repo_id = ?", (victim,)))
    assert kinds == {victim: "BuildPreparationFailed"}, kinds

    # 2. the run REACHED THE END. `_check_root_file_domain` compares `{repo: plan.dest}` against
    #    the SQLite-derived domain on every wave: had the re-plan failure popped either one, the
    #    two would no longer agree and `RootFileDomainDriftError` would have taken the whole run
    #    down. Reaching a per-repo exit 7 rather than that is the observable difference.
    assert "waves" in payload(result), result.output
    assert payload(result)["waves"] != [], f"no wave was driven at all: {payload(result)}"

    # 3. the NEGATIVE: the dest was not popped. The root files on the branch at the end still
    #    describe the failed unit, exactly as they did before it failed — which is what "stays in
    #    the domain" means from outside the process, and is the assertion the pre-domain test
    #    makes in reverse.
    published = published_root_files(monorepo)
    assert published, "nothing was published, so the presence below proves nothing"
    still_naming = sorted(path for path, text in published.items() if dest in text)
    assert still_naming == naming, (
        f"the root files naming `{dest}` changed across its re-plan failure ({naming} -> "
        f"{still_naming}): a post-domain failure must not rewrite the run's domain"
    )

    # 4. and the same claim from inside, where a pop of BOTH `plans` and `domain` would show. The
    #    guard the LATER wave ran — the one after the refusal — was handed the failed unit in
    #    `domain` and in `plans`, and NOT in `dispatched`.
    assert len(spy.calls) > 1, (
        f"the containment guard ran {len(spy.calls)} time(s), so no wave after the refusal was "
        f"checked and the assertions below would read the pre-failure state: {spy.calls}"
    )
    covered, dispatched, checked_domain = spy.calls[-1]
    assert checked_domain.get(victim) == dest, (
        f"the run's domain no longer maps {victim} -> `{dest}` at the wave its re-plan failed "
        f"in: a post-domain failure must not shrink the domain (ADR-0055): {checked_domain}"
    )
    assert victim in covered, (
        f"{victim} was dropped from `plans`, so the fleet-wide root files no longer describe it "
        f"and a re-resolve in this run would publish files naming fewer repos than the branch "
        f"holds: {covered}"
    )
    assert victim not in dispatched, (
        f"{victim} is still in the containment check's dispatch set although its worktree could "
        f"not be re-cut, which fails the whole run over a repo nothing is going to build: "
        f"{dispatched}"
    )
    assert set(dispatched) == set(siblings), (
        f"the wave dispatched {sorted(dispatched)} rather than {sorted(siblings)}: the exclusion "
        "must cost exactly the failed repo"
    )


def test_a_replan_failure_at_a_later_wave_does_not_take_the_rest_of_the_fleet_down(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    filter_repo: FakeFilterRepo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of ADR-0055's post-domain class: the failure costs exactly its own repo.

    Containment (§11.1), stated where it is hardest to hold. The failure happens *inside the wave
    loop*, after a lease-bearing wave has already run, and the repo it happened to stays in
    `plans` with a stale plan and in `domain` with a live `dest`. Three ways that could have gone
    wrong and none of them is visible from the failing repo's own row: the wave could abort, its
    wave-MATES could be withheld along with it, or `_check_root_file_domain` could refuse the
    whole wave over the domain entry that was deliberately kept.

    So the claim is asserted from the survivors' side: every other repo in the fleet — including
    the one that shares the failed repo's wave — is `SUCCEEDED` and has its generated package on
    the branch, while the failed one has none, because it was never leased. And the sibling's own
    build worktree contains the failed unit's `dest`, which is the containment half of
    `_check_root_file_domain` passing for a real reason rather than being skipped: the root files
    that sibling built against name a directory, and the directory is there.
    """
    hook, victim, siblings = _refuse_a_later_waves_replan(fleet, monorepo, monkeypatch)
    dest = DESTINATIONS[victim]

    result = build(fleet, "--no-sandbox")
    assert result.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION, result.output
    assert hook.calls.count(victim) == 2, hook.calls

    statuses = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 3"))
    assert {repo: status for repo, status in statuses.items() if repo != victim} == {
        repo: "SUCCEEDED" for repo in DESTINATIONS if repo != victim
    }, statuses

    packages = published_packages(monorepo)
    assert packages == {
        f"{other}/BUILD.bazel" for repo, other in DESTINATIONS.items() if repo != victim
    }, (
        f"the branch does not carry exactly the packages of every repo but {victim}: {packages}"
    )
    assert f"{dest}/BUILD.bazel" not in packages, (
        f"{victim} published a package although its worktree could not be re-cut for this wave"
    )

    # The wave-mate really built, and really built in a tree containing the unit that failed —
    # so the root files it was dispatched against are honest about the branch.
    for sibling in siblings:
        tree = build_worktree(fleet, sibling)
        assert (tree / dest).is_dir(), (
            f"{sibling} shares {victim}'s wave and built in a tree with no `{dest}`, so the root "
            "files in that tree name a directory it does not contain"
        )
        assert bazel.for_dest("build", DESTINATIONS[sibling]), (
            f"{sibling} was never dispatched, so its wave-mate's failure withheld it"
        )


def test_the_sandbox_really_wraps_the_build_in_a_networkless_container(
    fleet: Path, monorepo: Path, bazel: FakeBazel  # noqa: F811
) -> None:
    """Without `--no-sandbox` the command executed is a `docker run --network=none` wrapper.

    ADR-0010's whole claim is that a build which "passes" by fetching a dependency from the
    internet is impossible by construction. That claim is a property of the argv, so it is
    asserted on the argv; whether Docker is installed is a separate question this host cannot
    answer, and the seam is what keeps the two apart.
    """
    transformed(fleet)
    result = build(fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    dest = DESTINATIONS["acme-lib-ts"]
    call = bazel.for_dest("build", dest)[0]
    assert call.containerised, call.argv
    assert "--network=none" in call.argv, call.argv
    assert any(arg.startswith("--user=") for arg in call.argv), call.argv
    assert any(arg.startswith("--memory=") for arg in call.argv), call.argv
    # The tree really is bind-mounted, and the read-write dependency caches with it (§3.4).
    assert any(
        arg.startswith("--volume=") and str(build_worktree(fleet, "acme-lib-ts")) in arg
        for arg in call.argv
    ), call.argv
    # …and bazel is TOLD about those caches, in the container's terms. Mounting them and never
    # naming them is what made them inert: `--network=none` plus no repository cache is a build
    # that cannot resolve a single module, because there is nothing to fetch from and nothing to
    # fall back to. The flag values are read back out of the `--volume=` targets, so a rename on
    # either side fails here rather than pointing bazel at a path the container does not have.
    mounted = dict(
        arg.removeprefix("--volume=").split(":")[:2]
        for arg in call.argv
        if arg.startswith("--volume=")
    )
    assert mounted[str((fleet / "cache/bazel/disk").resolve())] == "/cache/disk", call.argv
    assert mounted[str((fleet / "cache/bazel/repo").resolve())] == "/cache/repo", call.argv
    assert call.bazel == (
        "bazel",
        "build",
        f"//{dest}/...",
        "--keep_going",
        "--build_event_json_file=bazel-build-events.json",
        f"--disk_cache={mounted[str((fleet / 'cache/bazel/disk').resolve())]}",
        f"--repository_cache={mounted[str((fleet / 'cache/bazel/repo').resolve())]}",
    ), call.bazel

    # …and the image was asked for a C compiler first, through the DRIVER's own payload rather
    # than a hand-built one — `_c_toolchain_gate` fires on exactly this `--sandbox` path and on
    # no other. Without one, `@@rules_go+//:stdlib` makes every target above unanalysable.
    assert bazel.probes, "the sandboxed build never probed the image for a C compiler"
    for probe in bazel.probes:
        assert probe.containerised, probe.argv
        assert probe.argv[-3:] == ("sh", "-c", C_TOOLCHAIN_PROBE), probe.argv
        assert probe.argv[-4] == VerifySection().container_image, probe.argv


# ---------------------------------------------------------------------------------------
# 2. a failure is structured, and it costs exactly its own repo
# ---------------------------------------------------------------------------------------


def test_a_build_failure_is_structured_and_does_not_take_its_siblings_down(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolver: FakeResolver,
) -> None:
    """One repo's `bazel build` exits 34; that repo ends `REQUIRES_HUMAN_INTERVENTION` with the
    exit code and a TRUNCATED stderr on its `attempts` row, and every sibling still `SUCCEEDED`.

    Two properties, and both are easy to lose. Per-repo isolation (§11.1) is the one that turns a
    single broken package into a fleet stop the moment a runner cancels siblings on a child
    failure. And `TruncatedStr` is the one that turns a 40 KB Bazel stderr into an attempt that
    is *never persisted* if the model rejects instead of truncating — the failure would then have
    no exit code, no output and no row, which is the opposite of Rule 11.
    """
    fake = FakeBazel(
        fleet / "artifacts" / "fake-bazel", fail={("build", DESTINATIONS["acme-app-ts"]): 34}
    )
    monkeypatch.setattr(cli, "BAZEL_RUNNER", fake)
    monkeypatch.setattr(cli, "FILTER_REPO_RUNNER", FakeFilterRepo())

    transformed(fleet)
    result = build(fleet, "--no-sandbox")
    assert result.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION, result.output

    statuses = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 3"))
    assert statuses["acme-app-ts"] == "REQUIRES_HUMAN_INTERVENTION", statuses
    assert {repo: statuses[repo] for repo in statuses if repo != "acme-app-ts"} == {
        repo: "SUCCEEDED" for repo in DESTINATIONS if repo != "acme-app-ts"
    }, statuses

    failed = [
        row
        for row in attempts(fleet, 3)
        if row["repo_id"] == "acme-app-ts" and row["exit_code"] != 0
    ]
    assert failed, attempts(fleet, 3)
    assert {row["exit_code"] for row in failed} == {34}, failed
    assert {row["failure_class"] for row in failed} == {"BUILD_ERROR"}, failed
    for row in failed:
        tail = str(row["stderr_tail"])
        assert tail, "a build failure with no output is not evidence"
        # Truncated, and SAYING SO — the 49 KB the fake emitted is longer than `LOG_TAIL_BYTES`,
        # so what reaches SQLite is the last 32 KiB verbatim plus the marker. Not a summary, not
        # a rejection: the row exists, and a human can see what was dropped.
        assert tail.endswith("bytes]"), tail[-80:]
        kept, _, note = tail.rpartition("\n[truncated ")
        assert note.endswith(" bytes]") and int(note.split()[0]) > 0, note
        assert LOUD_STDERR.endswith(kept), "the kept tail must be verbatim, not a summary"
        assert len(kept) < len(LOUD_STDERR), "the tail was not truncated"
    # `WorkerError.artifact_ref` is the FULL stream and §6 gives it no column, so what is
    # asserted here is that the path the runner advertised really holds the whole of it — the
    # tail is what a human skims and the file is what the ADR-0021 repair rung is prompted with.
    streams = list((fleet / "artifacts" / "fake-bazel").glob("*.err"))
    assert any(path.read_text(encoding="utf-8") == LOUD_STDERR for path in streams), streams

    # The repo that failed never published, so its package is absent from the branch tip while
    # its siblings' are there — containment is a fact about the merged tree, not only a status.
    listed = git(monorepo, "ls-tree", "-r", "--name-only", "integration").splitlines()
    assert f"{DESTINATIONS['acme-app-ts']}/BUILD.bazel" not in listed, listed
    assert f"{DESTINATIONS['acme-lib-ts']}/BUILD.bazel" in listed, listed


def test_a_later_invocations_build_sweep_reads_a_real_row_but_writes_nothing_new(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolver: FakeResolver,
) -> None:
    """D126 / ADR-0130 judgment call 3 (controller ruling, task-84): `_build_impl` also gets
    `_repropagate_terminal_providers`'s sweep, for defensive uniformity.

    **Corrected, round VI task-84 fix round 2 (opus-tier review, F2) — the earlier version of this
    test drove only ONE `build()` call, over a leaf failure (`acme-app-ts`, no dependent) — a
    fixture in which the sweep's own SELECT is trivially, structurally guaranteed to read zero
    rows (nothing has failed yet at the point in a FIRST invocation where the sweep runs, before
    the wave loop). That proved the SELECT is reachable-and-zero in a case where a non-zero
    reading was never possible, which is CLAUDE.md's own "validate what the instrument watches"
    guardrail failing in the direction it warns against — measured directly by an independent
    review, which drove a real second `build()` invocation over a real PROVIDER/DEPENDENT pair
    (mirrors this file's own `_STUB_PROVIDER`/`_STUB_CONSUMER`, `acme-lib-py`/`acme-app-py`, minus
    any stub row) and got `SWEEP_ROWCOUNTS_INVOCATION1=[0, 0]` then
    `SWEEP_ROWCOUNTS_INVOCATION2=[1]` — the SELECT genuinely returns a row on the second
    invocation, because `acme-lib-py` really is still `REQUIRES_HUMAN_INTERVENTION` on record.**

    **The true, narrower claim (ADR-0130's own actual rationale) is that the WRITE is redundant,
    not that the SELECT is a no-op.** `acme-app-py`'s BUILD row is already correctly `BLOCKED` by
    the FIRST invocation's own live containment (`_eligible_build_units`'s whole-fleet,
    `--wave`-independent PASS 1 already gave it a row before `acme-lib-py` ever dispatched, so
    `PhaseRunner._contain` found it and blocked it for real, in that SAME invocation). The SECOND
    invocation's sweep finds `acme-lib-py` still RHI (a real, non-zero SELECT) and attempts to
    write `blocked_by` again, but `append_blocked_by`'s illegal `BLOCKED -> BLOCKED` self-edge
    silently skips it — the row's `status`/`blocked_by` are unchanged, which is what this test now
    asserts directly rather than inferring from an unreachable zero.

    Now drives TWO real `build()` invocations over `_STUB_PROVIDER`/`_STUB_CONSUMER` (no stub row
    — the plain D126 case, not §37 Blocker C's). The `cli._rows` spy is installed only AFTER the
    first `build()` returns (so it observes exactly the second invocation's sweep, the one the
    "provable no-op" claim is actually about — the first invocation's own SELECT is asserted
    unreachable-and-zero by construction above, not re-instrumented here) and asserts the SELECT's
    row count is non-zero at least once (reachability: `acme-lib-py` really is still RHI on
    record), while `acme-app-py`'s `(status, blocked_by)` is byte-identical before and after the
    second invocation (the write, not merely the read, is inert).
    """
    fake = FakeBazel(
        fleet / "artifacts" / "fake-bazel", fail={("build", DESTINATIONS[_STUB_PROVIDER]): 34}
    )
    monkeypatch.setattr(cli, "BAZEL_RUNNER", fake)
    monkeypatch.setattr(cli, "FILTER_REPO_RUNNER", FakeFilterRepo())

    transformed(fleet)
    first = build(fleet, "--no-sandbox")
    assert first.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION, first.output

    before = {
        repo_id: (status, blocked_by)
        for repo_id, status, blocked_by in query(
            fleet, "SELECT repo_id, status, blocked_by FROM phases WHERE phase = 3"
        )
    }
    assert before[_STUB_PROVIDER][0] == "REQUIRES_HUMAN_INTERVENTION", before
    assert before[_STUB_CONSUMER][0] == "BLOCKED", before
    before_consumer = before[_STUB_CONSUMER]

    seen: list[int] = []
    original_rows = cli._rows

    async def spy(conn: Any, sql: str, params: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
        result = await original_rows(conn, sql, params)
        if "DISTINCT repo_id" in sql and "status = 'REQUIRES_HUMAN_INTERVENTION'" in sql:
            seen.append(len(result))
        return result

    monkeypatch.setattr(cli, "_rows", spy)

    second = build(fleet, "--no-sandbox")
    assert second.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION, second.output

    assert seen, "the sweep's own SELECT never ran on the second invocation — instrument silent"
    assert any(count > 0 for count in seen), (
        "the SELECT read zero every time on a second invocation over a genuinely still-RHI "
        f"provider — the reachability this test exists to prove did not hold: {seen}"
    )

    after = {
        repo_id: (status, blocked_by)
        for repo_id, status, blocked_by in query(
            fleet, "SELECT repo_id, status, blocked_by FROM phases WHERE phase = 3"
        )
    }
    assert after[_STUB_CONSUMER] == before_consumer, (
        "the sweep's WRITE was not inert -- acme-app-py's (status, blocked_by) moved even though "
        f"it was already correctly BLOCKED: before={before_consumer} after={after[_STUB_CONSUMER]}"
    )


def test_the_build_side_sweep_respects_stub_blocked_from_a_resume_continuation(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolver: FakeResolver,
) -> None:
    """D126 / ADR-0130 fix round 2 (F3, opus-tier review): `_continue_impl` can drive BUILD in
    the SAME `fleet resume --stub-blocked` invocation as step 6's real unblock (`orchestrator.
    reentry.stub_permits_removal`), so `_build_impl`'s own sweep needs the identical `stub_blocked`
    exemption `_transform_impl`'s already had, or it silently re-blocks a dependent step 6 had just
    correctly freed — measured directly by an independent review, using this exact fixture shape.

    Mirrors this file's own Blocker C fixture (`acme-lib-py`/`acme-app-py`) minus any `ACTIVE`
    stub row, plus the same raw-SQL step-6 stand-in `test_an_active_stub_redirects_...` uses (this
    file's fixtures do not drive a real `fleet resume`, per that test's own disclosed reason — but
    `fleet build` has no `--stub-blocked` CLI flag at all, so the ONLY way to exercise the
    `stub_blocked=True` value `_continue_impl` threads is to call the exact function it calls,
    `_repropagate_terminal_providers`, directly with that value — which is what this test does,
    rather than approximating it through the CLI).

    Two calls, not one: `stub_blocked=False` (the plain `fleet build` path, and this task's own
    fix-round-0 behavior) DOES re-block the freed repo — the bug this fix round exists to close,
    reproduced directly rather than assumed — and `stub_blocked=True` (the real `_continue_impl`
    path) does NOT.
    """
    fake = FakeBazel(
        fleet / "artifacts" / "fake-bazel", fail={("build", DESTINATIONS[_STUB_PROVIDER]): 34}
    )
    monkeypatch.setattr(cli, "BAZEL_RUNNER", fake)
    monkeypatch.setattr(cli, "FILTER_REPO_RUNNER", FakeFilterRepo())

    transformed(fleet)
    first = build(fleet, "--no-sandbox")
    assert first.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION, first.output

    run_id = str(query(fleet, "SELECT run_id FROM runs")[0][0])
    statuses = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 3"))
    assert statuses[_STUB_CONSUMER] == "BLOCKED", statuses

    def _free_consumer() -> None:
        conn = sqlite3.connect(fleet / "state" / "fleet.db")
        try:
            conn.execute(
                "UPDATE phases SET status = 'PENDING', blocked_by = '[]' "
                " WHERE run_id = ? AND repo_id = ? AND phase = 3",
                (run_id, _STUB_CONSUMER),
            )
            conn.commit()
        finally:
            conn.close()

    settings = FleetSettings.load(fleet / "config")
    db_path = fleet / "state" / "fleet.db"

    async def _sweep(*, stub_blocked: bool) -> None:
        async with cli.StateWriter(db_path, owner="test") as writer:
            read_conn = await cli.connect_ro(db_path)
            try:
                await cli._repropagate_terminal_providers(
                    read_conn, writer, run_id, cli.Phase.BUILD, settings,
                    stub_blocked=stub_blocked,
                )
            finally:
                await read_conn.close()

    # Unguarded (`stub_blocked=False`): reproduces the bug directly. Run via `asyncio.run` in a
    # plain (non-`async def`) test, never inside an already-running loop: `build()`/`transformed()`
    # above go through `runner.invoke`, which itself calls `asyncio.run` internally, so an
    # `async def` test here would raise "asyncio.run() cannot be called from a running event loop"
    # the moment this function's own event loop tried to nest inside pytest-asyncio's.
    _free_consumer()
    asyncio.run(_sweep(stub_blocked=False))
    after_unguarded = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 3"))
    assert after_unguarded[_STUB_CONSUMER] == "BLOCKED", after_unguarded

    # Guarded (`stub_blocked=True`, the real `_continue_impl` value): the fix.
    _free_consumer()
    asyncio.run(_sweep(stub_blocked=True))
    after_guarded = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 3"))
    assert after_guarded[_STUB_CONSUMER] == "PENDING", after_guarded


# ---------------------------------------------------------------------------------------
# 3. the wave gate — Phase 4 never starts for a repo whose Phase 3 did not succeed
# ---------------------------------------------------------------------------------------


def test_phase_four_withholds_a_repo_whose_phase_three_did_not_succeed(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolver: FakeResolver,
) -> None:
    """A repo whose Phase 3 failed gets **no Phase 4 row and no Phase 4 attempt** — not a row
    that fails later.

    §3.4's precondition is "Phase 3 `SUCCEEDED`", and where it is enforced decides whether it
    works. `WaveScheduler.admit` reads `phases.status` and a missing row reads as `PENDING`, so a
    repo admitted-then-refused would hold its wave `OPEN` forever *and* consume an attempt on
    work it was never eligible for. Withheld before the row exists, it does neither.
    """
    fake = FakeBazel(
        fleet / "artifacts" / "fake-bazel", fail={("build", DESTINATIONS["acme-app-ts"]): 1}
    )
    monkeypatch.setattr(cli, "BAZEL_RUNNER", fake)
    monkeypatch.setattr(cli, "FILTER_REPO_RUNNER", FakeFilterRepo())

    transformed(fleet)
    assert build(fleet, "--no-sandbox").exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION

    result = verify(fleet, "--rdeps-limit", "3", "--rdeps-sample-n", "1")
    assert result.exit_code == ExitCode.SUCCESS, result.output
    body = payload(result)
    assert "acme-app-ts" in body["withheld"], body["withheld"]

    verified = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 4"))
    assert "acme-app-ts" not in verified, verified
    assert set(verified) == {repo for repo in DESTINATIONS if repo != "acme-app-ts"}, verified
    assert set(verified.values()) == {"SUCCEEDED"}, verified
    assert not [row for row in attempts(fleet, 4) if row["repo_id"] == "acme-app-ts"]


# ---------------------------------------------------------------------------------------
# 4. Phase 4 — the closure, and the disclosure that keeps a sampled closure honest
# ---------------------------------------------------------------------------------------


def test_a_truncated_closure_forces_closure_sampled_and_the_report_discloses_it(
    fleet: Path, monorepo: Path, bazel: FakeBazel  # noqa: F811
) -> None:
    """Over the bound: `CLOSURE_SAMPLED`, with the true count, the sample size and the seed in
    the report and in the operator-facing output. Under it: the closure is verified whole.

    This is the one place §3.4's success criterion has two readings, and they are one criterion
    only because the reduction is *declared*. A run that reported 40 000 untested targets as
    green and a run that stalled forever are the two failures the bound sits between, so what
    the test has to prove is not that sampling happened but that it is impossible to read the
    result as an unqualified pass: `equivalence` is derived by `VerificationReport` from
    `rdeps_truncated`, never supplied, and it is what forces the PR to a draft.
    """
    transformed(fleet)
    assert build(fleet, "--no-sandbox").exit_code == ExitCode.SUCCESS
    result = verify(fleet, "--rdeps-limit", "3", "--rdeps-sample-n", "1")
    assert result.exit_code == ExitCode.SUCCESS, result.output

    reports = payload(result)["reports"]
    sampled = reports["acme-lib-ts"]
    assert sampled["rdeps_truncated"] is True, sampled
    assert sampled["equivalence"] == "CLOSURE_SAMPLED", sampled
    assert sampled["rdeps_target_count"] == len(CLOSURES["ts/acme/lib"]), sampled
    assert sampled["rdeps_tested"] < sampled["rdeps_target_count"], sampled
    assert sampled["rdeps_sample_n"] == 1, sampled
    assert sampled["rdeps_sample_seed"], "an unreproducible sample is not auditable (§11.6)"
    assert sampled["verdict"] == "PASS", sampled
    assert "CLOSURE_SAMPLED" in result.stdout, result.stdout

    whole = reports["acme-app-py"]
    assert whole["rdeps_truncated"] is False, whole
    assert whole["equivalence"] == "FULL", whole
    assert whole["rdeps_tested"] == whole["rdeps_target_count"] == 1, whole

    # The tested labels really reached a `--target_pattern_file` rather than an argv explosion.
    pattern_file = Path(str(sampled["target_pattern_file"]))
    assert pattern_file.is_file(), sampled
    tested = pattern_file.read_text(encoding="utf-8").split()
    assert len(tested) == sampled["rdeps_tested"], tested
    assert set(tested) <= set(CLOSURES["ts/acme/lib"]), tested
    assert "//ts/acme/lib:lib" in tested, "every direct rdep survives sampling (§3.4)"
    assert any(
        arg == f"--target_pattern_file={pattern_file}"
        for call in bazel.commands("test")
        for arg in call
    ), bazel.commands("test")


def test_phase_four_reads_a_fresh_snapshot_that_carries_phase_threes_build_files(
    fleet: Path, monorepo: Path, bazel: FakeBazel  # noqa: F811
) -> None:
    """§3.4 step 1's "current integration branch tip" is a FRESH snapshot ref, and the tree it
    names really contains every repo's generated `BUILD.bazel`.

    The second half is what makes the first half mean anything. Phase 4 re-tests on the tip
    because dependencies merged since Phase 3 may have moved; if Phase 3's generated files were
    left uncommitted in a per-attempt worktree, that tip would be a tree no `bazel build
    //<dest>/...` could ever resolve, and a green Phase 4 over it would be green about nothing.
    """
    transformed(fleet)
    assert build(fleet, "--no-sandbox").exit_code == ExitCode.SUCCESS
    build_refs = {row["integration_ref"] for row in attempts(fleet, 3)}
    assert verify(fleet, "--rdeps-limit", "3").exit_code == ExitCode.SUCCESS

    verify_refs = {row["integration_ref"] for row in attempts(fleet, 4)}
    assert verify_refs, attempts(fleet, 4)
    assert all(ref is not None and ref.startswith("refs/fleet/") for ref in verify_refs)
    assert not (verify_refs & build_refs), "Phase 4 must cut its OWN snapshot, not reuse Phase 3's"

    for repo_id, dest in DESTINATIONS.items():
        tree = verify_worktree(fleet, repo_id)
        assert (tree / dest / "BUILD.bazel").is_file(), f"{repo_id}: {dest}/BUILD.bazel missing"
        assert (tree / "MODULE.bazel").is_file(), repo_id
    # ... and every repo's package is on the branch, not only in its own worktree.
    listed = git(monorepo, "ls-tree", "-r", "--name-only", "integration")
    for dest in DESTINATIONS.values():
        assert f"{dest}/BUILD.bazel" in listed.splitlines(), dest


def test_phase_four_reuses_the_bazel_caches_phase_three_just_filled(
    fleet: Path, monorepo: Path, bazel: FakeBazel  # noqa: F811
) -> None:
    """§3.4's bounds table puts the persistent build cache in the PHASE 4 section — and Phase 4's
    `bazel test` over the blast radius was the one invocation in the harness naming neither flag.

    Phase 3 mounted the caches and named them; `VerifyWorker._rdeps` simply never forwarded
    `cache_mounts`, which `VerifyInput` had carried all along. So the widest build in the
    pipeline — every reverse dependency of the package Phase 3 had just built, into a disk cache
    holding exactly those actions — re-executed all of it and re-fetched every module cold, on
    every attempt, for every repo. Not a missed cache hit: the §12 row this bound answers to is
    "build-verification cost explosion".

    The expected values come from `cli._cache_mounts` over this run's own settings rather than
    from literals, so Phase 3 and Phase 4 cannot drift onto two different directories — two
    caches that disagree cost what having none costs. HOST paths because `--no-sandbox` is the
    lane here and `rdepverify` never containerises at all.
    """
    transformed(fleet)
    assert build(fleet, "--no-sandbox").exit_code == ExitCode.SUCCESS
    assert verify(fleet, "--rdeps-limit", "3").exit_code == ExitCode.SUCCESS

    mounts = cli._cache_mounts(FleetSettings.load(fleet / "config"))
    expected = [mount.flag(sandboxed=False) for mount in mounts]
    assert expected == [
        f"--disk_cache={(fleet / 'cache/bazel/disk').resolve()}",
        f"--repository_cache={(fleet / 'cache/bazel/repo').resolve()}",
    ], expected

    rdeps_tests = [
        argv
        for argv in bazel.commands("test")
        if any(a.startswith("--target_pattern_file=") for a in argv)
    ]
    assert rdeps_tests, [c.bazel for c in bazel.calls]
    for argv in rdeps_tests:
        assert [a for a in argv if a.startswith(("--disk_cache=", "--repository_cache="))] == (
            expected
        ), argv
    # The rdeps *queries* are the remaining Phase 4 bazel invocations, and they used to carry no
    # cache flag at all — recorded here as a deliberate deferral, since changing `bazel/query.py`'s
    # argv was its own decision. It has now been made, on a measurement against the vendored
    # 9.2.0: both flags parse for `query` (`canonicalize-flags --for_command=query` echoes them
    # back), but a probe query wrote 2.3 MB into `--repository_cache` and NOT ONE FILE into
    # `--disk_cache` — `query` executes no actions and a disk cache holds nothing else. So the
    # query gets the repository cache, which is the fetch it repeats, and not the disk cache,
    # which would be a flag implying a hit that cannot happen.
    queries = bazel.commands("query")
    assert queries, "the closure query must still have run"
    repository_only = [f for f in expected if f.startswith("--repository_cache=")]
    assert len(repository_only) == 1, expected
    for argv in queries:
        assert [
            a for a in argv if a.startswith(("--disk_cache=", "--repository_cache="))
        ] == repository_only, argv


# ---------------------------------------------------------------------------------------
# 5. idempotency — the merge guard and the wave guard, both by git
# ---------------------------------------------------------------------------------------


def test_re_running_build_duplicates_no_merge_no_commit_and_no_file(
    fleet: Path, monorepo: Path, bazel: FakeBazel  # noqa: F811
) -> None:
    """A second `fleet build` changes nothing, and a second one forced back into a settled wave
    re-ingests without duplicating the merge.

    Two guards, deliberately tested together because each hides the other's absence. The wave
    guard is SQLite's: a `SUCCEEDED` phase is never re-admitted, so the plain re-run drives no
    wave at all — which would also be the observed behaviour if the *git* guard were missing. So
    the second half forces the wave with `--wave` and asserts on git: `already_ingested` matches
    the ADR-0011 trailer pair and merges nothing, which is the only guard a `fleet resume` after
    a crash mid-wave can actually rely on.
    """
    transformed(fleet)
    assert build(fleet, "--no-sandbox").exit_code == ExitCode.SUCCESS
    first_tip = git(monorepo, "rev-parse", "integration")
    first_merges = merge_trailers(monorepo)
    files = {
        repo: (build_worktree(fleet, repo) / dest / "BUILD.bazel").read_text(encoding="utf-8")
        for repo, dest in DESTINATIONS.items()
    }
    calls = len(bazel.calls)

    again = build(fleet, "--no-sandbox")
    assert again.exit_code == ExitCode.SUCCESS, again.output
    assert payload(again)["waves"] == [], payload(again)
    assert len(bazel.calls) == calls, "a settled wave must dispatch nothing"
    assert git(monorepo, "rev-parse", "integration") == first_tip

    forced = build(fleet, "--no-sandbox", "--wave", "0")
    assert forced.exit_code == ExitCode.SUCCESS, forced.output
    assert merge_trailers(monorepo) == first_merges, "the ingest guard let a merge through twice"
    assert git(monorepo, "rev-parse", "integration") == first_tip
    for repo, dest in DESTINATIONS.items():
        assert (
            build_worktree(fleet, repo) / dest / "BUILD.bazel"
        ).read_text(encoding="utf-8") == files[repo], repo


def test_build_refuses_without_a_monorepo_to_merge_into(fleet: Path) -> None:  # noqa: F811
    """§3.3's precondition, refused with exit 2 and not with a traceback.

    Exit 2 rather than 1 because nothing is wrong with the harness: the operator has to create
    the repository at `run.monorepo_path` or point the key at it. A verb that created it instead
    could never distinguish a misconfiguration from a first run, and the first merge would land
    somewhere no later phase knows to look.
    """
    transformed(fleet)
    result = build(fleet, "--no-sandbox", json_output=False)
    assert result.exit_code == ExitCode.USAGE, result.output
    assert "run.monorepo_path" in result.output, result.output


def test_stub_blocked_on_build_is_a_no_op_when_no_stub_exists(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    filter_repo: FakeFilterRepo,
) -> None:
    """`fleet build --stub-blocked` USED to refuse (round VI task 69, §12.37 Leg 3 replaces this).

    OLD assertion (pre-task-69): `build(fleet, "--stub-blocked").exit_code == ExitCode.USAGE` —
    PASSED on `main` before this round (`_validate_build_flags` raised `UsageError`
    unconditionally) and FAILS after it (the refusal is removed once task-67's TRANSFORM-phase
    creation and task-68's BUILD-phase render both landed).

    NEW behaviour: `--stub-blocked` on `fleet build` is accepted and does nothing distinguishable
    — task-68's rendering and this leg's `stub_degrade_transform(phase=Phase.BUILD)` correction
    are both unconditional and data-driven off the `stubs` table, never off this flag
    (`_validate_build_flags`'s own docstring). On a fleet with no `stubs` row at all, the build
    must refuse for the SAME reason a plain `fleet build` here does (no monorepo to merge into,
    `test_build_refuses_without_a_monorepo_to_merge_into` above) — proving the flag changed
    nothing, not that the command started succeeding.

    A monorepo, `FakeFilterRepo` and `FakeBazel` are all provisioned (`monorepo`/`filter_repo`/
    `bazel` fixtures) precisely so `ExitCode.USAGE` cannot mean "no monorepo to merge into" here —
    the only refusal that could fire is the one under test, and a plain `fleet build` on the same
    transformed fixture reaches `ExitCode.SUCCESS`.
    """
    _ = bazel  # the default (all-green) FakeBazel; present only to avoid a real bazel dependency
    transformed(fleet)
    plain = build(fleet, "--no-sandbox", json_output=False)
    assert plain.exit_code == ExitCode.SUCCESS, plain.output
    dests = relocations(filter_repo)

    def _bazel_files() -> dict[str, str]:
        return {
            repo: (build_worktree(fleet, repo) / dest / "BUILD.bazel").read_text(
                encoding="utf-8"
            )
            for repo, dest in dests.items()
        }

    before = _bazel_files()
    stubbed = build(fleet, "--no-sandbox", "--stub-blocked", "--wave", "0", json_output=False)
    assert stubbed.exit_code != ExitCode.USAGE, stubbed.output
    assert stubbed.exit_code == ExitCode.SUCCESS, stubbed.output
    # A settled wave dispatches nothing regardless of the flag — the SAME idempotency
    # `test_build_is_idempotent_and_the_second_invocation_dispatches_nothing` proves for a plain
    # re-run; `--stub-blocked` must not be the thing that makes a second `fleet build` re-dispatch.
    assert _bazel_files() == before


def test_no_rdeps_is_refused_rather_than_reporting_a_blast_radius_of_zero(
    fleet: Path,  # noqa: F811
) -> None:
    """`--no-rdeps` names a verification that is not one.

    §3.4's success criterion IS `bazel test` over the verified target set. A run that skipped the
    closure would report a blast radius of zero as proven, which is the exact failure the
    `rdeps_limit`/sampling machinery exists to avoid having to choose.
    """
    scanned(fleet)
    result = verify(fleet, "--no-rdeps", json_output=False)
    assert result.exit_code == ExitCode.USAGE, result.output
    assert "--no-rdeps" in result.output, result.output


# ---------------------------------------------------------------------------------------
# 6. the ecosystem adapters, reached through the whole chain (§3.3 step 2, §7.5, ADR-0020)
# ---------------------------------------------------------------------------------------


def test_a_jvm_repo_and_a_js_repo_generate_real_ecosystem_appropriate_targets(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    filter_repo: FakeFilterRepo,
) -> None:
    """`java_library` for the Maven module, `ts_project` for the npm package — with their loads,
    their external labels and NO `filegroup` anywhere.

    **Why this is the headline.** Until the registry was wired in, every repo in every end-to-end
    run produced the same degraded shape: one `filegroup`, no `load()`, no external dependency.
    So the entire BUILD-generation path — `render_build_bazel`, the load-statement collection, the
    dep-label spelling, `BuildPlan`'s "an adapter emitted no targets" guard — had only ever been
    exercised against a fallback, and a `java_library` that failed to render would have looked
    exactly like a run with no JVM repos in it. Asserting on the generated TEXT rather than on a
    plan or a finding is deliberate: the file is what Bazel reads, and every layer between the
    adapter and the file can succeed against a file nobody wrote.
    """
    add_repos(fleet, ["acme-commons-java", "acme-ui-ts"])
    transformed(fleet)
    result = build(fleet, "--no-sandbox")
    assert result.exit_code == ExitCode.SUCCESS, result.output

    dests = relocations(filter_repo)
    jvm = (build_worktree(fleet, "acme-commons-java") / dests["acme-commons-java"]).joinpath(
        "BUILD.bazel"
    )
    body = jvm.read_text(encoding="utf-8")
    assert "filegroup(" not in body, body
    assert 'load("@rules_java//java:defs.bzl", "java_library")' in body, body
    assert "java_library(" in body, body
    assert 'name = "commons"' in body, body
    assert '"src/main/java/com/acme/commons/Widget.java"' in body, body
    assert '"@maven//:guava"' in body, body

    js = (build_worktree(fleet, "acme-ui-ts") / dests["acme-ui-ts"]) / "BUILD.bazel"
    body = js.read_text(encoding="utf-8")
    assert "filegroup(" not in body, body
    # The SYMBOL out of that `.bzl`, not the whole `load` line: `render_build_bazel` merges every
    # symbol taken from one label into a single sorted `load`, so an adapter that legitimately
    # starts emitting a second rule from `ts:defs.bzl` rewrites this line without changing
    # anything this test is about. That is the same failure mode as a hardcoded version pin.
    assert re.search(r'load\("@aspect_rules_ts//ts:defs\.bzl",[^)]*"ts_project"', body), body
    assert "ts_project(" in body, body
    assert 'name = "ui"' in body, body
    # `//<dest>:node_modules/left-pad`, not `@npm//:left-pad` and no longer `//:…`: rules_js
    # exposes an npm package as a LINK created by `npm_link_all_packages()`, and it creates that
    # link once per pnpm IMPORTER (ADR-0048) — so with one importer per JS repo the link this
    # repo depends on lives in this repo's own package. The hub's own root package declares no
    # packages at all ("target 'left-pad' not declared in package ''"), and a root-package
    # spelling now gets the same message about `package ''`. The label and the macro that makes
    # it exist are one decision, so both are the adapter's — and both are asserted here.
    assert f'"//{dests["acme-ui-ts"]}:node_modules/left-pad"' in body, body
    assert "npm_link_all_packages(" in body, body
    # `src/index.ts` is one of the JS adapter's declared entrypoints, so the adapter's *second*
    # target rides along — proof the driver takes the adapter's whole answer and not its first row.
    assert "js_binary(" in body, body

    # …and none of it came from a branch in the driver: the two files disagree about every rule
    # name, every load label and every external repo, which is what "the adapter decides" means.
    assert result.exit_code == ExitCode.SUCCESS
    assert payload(result)["adapter_unavailable"] == [], result.output


def test_a_python_repo_with_a_real_test_file_gets_a_real_py_test_target(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    filter_repo: FakeFilterRepo,
) -> None:
    """D112, over `FakeBazel`: a Python repo with a real `test_*.py` gets a real `py_test`
    target, and the test file is NOT also a `py_library` source.

    **The discriminator.** Before round VI task 53's fix, `_plan_build` never populated
    `BuildUnit.test_srcs` — `cli.py:_plan_build` passed only `srcs=list(srcs)`, so it defaulted
    to `[]` (`models/build.py`'s `Field(default_factory=list)`) — and `test_sources()`
    (`ecosystems/base.py`) filters exactly that empty list, so `py.py:test_targets()`'s `if not
    test_srcs: return []` guard always fired. `generate_targets()`'s `py_library` swallowed
    `acme_widgets_py/test_widgets.py` into its OWN `srcs` instead (nothing partitioned it out),
    because `sources()` reads `unit.srcs` and nothing upstream removed the test file from it. So
    pre-fix this assertion set is exactly reversed: no `py_test(` in the body, and the test file
    present in the `py_library` `srcs=[...]` list. This is `D112` in
    `docs/INTEGRATION_HONESTY.md`, and the report for this task records the revert-and-rerun that
    proves it (`git stash` the `cli.py` hunk, rerun this test, RED; restore it, GREEN).
    """
    add_repos(fleet, ["acme-widgets-py"])
    transformed(fleet)
    result = build(fleet, "--no-sandbox")
    assert result.exit_code == ExitCode.SUCCESS, result.output

    dest = relocations(filter_repo)["acme-widgets-py"]
    body = (build_worktree(fleet, "acme-widgets-py") / dest / "BUILD.bazel").read_text(
        encoding="utf-8"
    )
    assert "py_test(" in body, body
    assert 'name = "acme-widgets-py_test"' in body, body
    assert '"acme_widgets_py/test_widgets.py"' in body, body
    assert 'main = "acme_widgets_py/test_widgets.py"' in body, body

    # The library target still exists (`__init__.py` is real library source) but its `srcs` no
    # longer swallows the test file — that is the "not double-declared" half of the claim, and
    # the half a test asserting ONLY "a `py_test` exists" would miss entirely.
    library_start = body.index("py_library(")
    test_start = body.index("py_test(")
    library_body = body[library_start:test_start]
    assert '"acme_widgets_py/__init__.py"' in library_body, library_body
    assert '"acme_widgets_py/test_widgets.py"' not in library_body, library_body


def test_a_jvm_repo_with_a_real_test_file_gets_a_real_java_test_target(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    filter_repo: FakeFilterRepo,
) -> None:
    """D112 (round VI task 70), over `FakeBazel`: a JVM repo with a real `src/test/java` class
    gets a real `java_test` target, and the test file is NOT also a `java_library` source.

    **The discriminator.** Before this task's fix, `TEST_SRC_PARTITIONED_ECOSYSTEMS`
    (`ecosystems/base.py`) had no `MAVEN`/`GRADLE` member, so `_partition_test_srcs` always took
    its `ecosystem not in TEST_SRC_PARTITIONED_ECOSYSTEMS` branch for JVM units and returned
    `(list(srcs), [])` unconditionally — `test_srcs` stayed `()`, `jvm.py:test_targets()`'s `if
    not test_srcs: return []` guard always fired, and `generate_targets()`'s `java_library`
    swallowed `WidgetTest.java` into its own `srcs` instead (nothing partitioned it out, since
    `sources()` reads `unit.srcs` verbatim). So pre-fix this assertion set is exactly reversed: no
    `java_test(` in the body, and the test file present in `java_library`'s `srcs=[...]` list.
    This is the JVM half of `D112` in `docs/INTEGRATION_HONESTY.md`, and this task's report
    records the revert-and-rerun that proves it.
    """
    add_repos(fleet, ["acme-widgets-jvm"])
    transformed(fleet)
    result = build(fleet, "--no-sandbox")
    assert result.exit_code == ExitCode.SUCCESS, result.output

    dest = relocations(filter_repo)["acme-widgets-jvm"]
    body = (build_worktree(fleet, "acme-widgets-jvm") / dest / "BUILD.bazel").read_text(
        encoding="utf-8"
    )
    assert "java_test(" in body, body
    assert 'name = "widgets_test"' in body, body
    assert '"src/test/java/com/acme/widgets/WidgetTest.java"' in body, body
    assert '"@maven//:junit"' in body, body

    # The library target still exists (`Widget.java` is real library source) but its `srcs` no
    # longer swallows the test file — the half a test asserting ONLY "a `java_test` exists"
    # would miss entirely.
    library_start = body.index("java_library(")
    test_start = body.index("java_test(")
    library_body = body[library_start:test_start]
    assert '"src/main/java/com/acme/widgets/Widget.java"' in library_body, library_body
    assert "WidgetTest.java" not in library_body, library_body


def test_a_js_repo_with_a_real_test_file_gets_a_real_js_test_target(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    filter_repo: FakeFilterRepo,
) -> None:
    """D112 (round VI task 87), over `FakeBazel`: a JS/TS repo with a real `*.test.ts` gets a real
    `js_test` target backed by a compiled `ts_project`, and the test file is NOT also part of the
    library `ts_project`'s `srcs`.

    **The discriminator.** Before this task's fix, `TEST_SRC_PARTITIONED_ECOSYSTEMS`
    (`ecosystems/base.py`) had no `NPM` member, so `_partition_test_srcs` always took its
    `ecosystem not in TEST_SRC_PARTITIONED_ECOSYSTEMS` branch for JS units and returned
    `(list(srcs), [])` unconditionally — `test_srcs` stayed `()`, `js.py:test_targets()`'s `if not
    test_srcs: return []` guard always fired, and `generate_targets()`'s `ts_project` swallowed
    `src/index.test.ts` into its own `srcs` instead (nothing partitioned it out, since `sources()`
    reads `unit.srcs` verbatim). So pre-fix this assertion set is exactly reversed: no `js_test(`
    in the body, and the test file present in the library `ts_project`'s `srcs=[...]` list. This
    is the JS half of `D112` in `docs/INTEGRATION_HONESTY.md`, and this task's report records the
    revert-and-rerun that proves it.

    **Two targets, not one — the shape D7's precedent alone did not anticipate.** Unlike
    Python/JVM, `js_test` cannot compile `.ts` itself (it is a `rules_js` *runtime* rule, exactly
    like `js_binary`), so the fix also emits a `{name}_test_lib` `ts_project` compiling the test
    file, and `js_test` depends on it via `data=` rather than `deps=`/`srcs=` — neither of which
    exists on `js_test` (see `js.py::test_targets()`'s own docstring).

    **Two further discriminators, added in fix round 1 after review found the first version of
    this fix reintroduced the failure it existed to close.** Both need the fixture's `__tests__/`
    pair and neither is expressible without it:

    * `entry_point` must be `src/index.test.js`, NOT `__tests__/helper.js`. The pre-fix-round
      selector took `test_srcs[0]`, and `package_relative` sorts, so `__tests__/…` won on every
      repo with that directory — the emitted `js_test` ran a helper, or (with a `.json` fixture
      first) a file Node cannot execute at all. `--nobuild` analysis is blind to this by
      construction: the wrong label resolves perfectly well, so only an assertion on WHICH file
      was selected can catch it, which is why it lives here and not only in `test_bazel.py`.
    * `__tests__/__snapshots__/index.test.ts.snap` must still appear, carried as the library
      `ts_project`'s `data`. The pre-fix-round `__tests__/` predicate matched it, moved it out of
      `srcs` into `test_srcs`, and `test_sources()`'s `accepts_src` then dropped it while
      `non_source_files()` (reading `unit.srcs` alone) could no longer see it — the file vanished
      from the generated package entirely, against `base.py::non_source_files`'s own stated
      contract that refused files are carried rather than dropped.
    """
    add_repos(fleet, ["acme-widgets-ts"])
    transformed(fleet)
    result = build(fleet, "--no-sandbox")
    assert result.exit_code == ExitCode.SUCCESS, result.output

    dest = relocations(filter_repo)["acme-widgets-ts"]
    body = (build_worktree(fleet, "acme-widgets-ts") / dest / "BUILD.bazel").read_text(
        encoding="utf-8"
    )
    assert "js_test(" in body, body
    assert 'name = "acme-widgets-ts_test"' in body, body
    assert 'name = "acme-widgets-ts_test_lib"' in body, body
    assert '"src/index.test.ts"' in body, body
    # D7, widened: neither `deps` nor `srcs` may appear on the `js_test` block itself.
    test_start = body.index("js_test(\n")
    test_end = body.index("\n)", test_start) + len("\n)")
    js_test_body = body[test_start:test_end]
    assert "deps" not in js_test_body, js_test_body
    assert "srcs" not in js_test_body, js_test_body
    assert 'data = [\n        ":acme-widgets-ts_test_lib",\n    ],' in js_test_body, js_test_body

    # The library `ts_project` still exists (`src/index.ts` is real library source) but its
    # `srcs` no longer swallows the test file — the half a test asserting ONLY "a `js_test`
    # exists" would miss entirely.
    library_start = body.index("ts_project(")
    library_body = body[library_start:test_start]
    assert '"src/index.ts"' in library_body, library_body
    assert '"src/index.test.ts"' not in library_body, library_body

    # Fix round 1, discriminator 1: the entry point is the TEST, not the alphabetically-first
    # path. `__tests__/helper.ts` sorts first and is what the pre-fix-round selector chose.
    assert 'entry_point = "src/index.test.js"' in js_test_body, js_test_body
    assert "__tests__" not in js_test_body, js_test_body
    # ...and the helper is still COMPILED, just not run: dropping it would be the sibling defect.
    lib_start = body.index('name = "acme-widgets-ts_test_lib"')
    assert '"__tests__/helper.ts"' in body[lib_start:], body[lib_start:]

    # Fix round 1, discriminator 2: a refused file under `__tests__/` is CARRIED, not dropped.
    # `base.py::non_source_files` — "Refused files are carried, not dropped".
    snap = '"__tests__/__snapshots__/index.test.ts.snap"'
    assert snap in body, f"the snapshot vanished from the generated package entirely:\n{body}"
    assert snap in library_body, (
        "the snapshot must be carried by the LIBRARY ts_project's `data` (it is refused by "
        f"`accepts_src`, so it can live nowhere else):\n{library_body}"
    )


def test_destinations_come_from_the_adapters_not_from_the_driver(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    filter_repo: FakeFilterRepo,
) -> None:
    """A Maven module lands under the JVM adapter's directory and a PyPI distribution under the
    Python adapter's — with no `dest:` in the manifest and no directory named in `cli.py`.

    **Why:** `repos.dest_path` used to be mandatory, so a fleet of 250 repos meant 250 hand-written
    destinations and §3.3's `layout(repo)` was dead code. The expected paths below are built from
    `adapter.monorepo_dir` and `adapter.path_tail()` rather than typed out, so re-pointing an
    adapter moves this assertion with it (§13 row 33) instead of turning it red.
    """
    add_repos(fleet, ["acme-commons-java", "acme-tool-py"])
    transformed(fleet)
    assert build(fleet, "--no-sandbox").exit_code == ExitCode.SUCCESS

    ecosystems.discover()
    dests = relocations(filter_repo)
    for repo_id, group, name in (
        ("acme-commons-java", "com.acme", "commons"),
        ("acme-tool-py", "", "acme-tool-py"),
    ):
        adapter = ecosystems.for_ecosystem(repo_ecosystem(fleet, repo_id))
        coordinate = Coordinate(
            ecosystem=repo_ecosystem(fleet, repo_id), group=group, name=name
        )
        expected = f"{adapter.monorepo_dir}/{adapter.path_tail(coordinate)}"
        assert dests[repo_id] == expected, (repo_id, dests[repo_id], expected)
        # …and the tree really is there, merged, in the worktree the build read.
        assert (build_worktree(fleet, repo_id) / expected / "BUILD.bazel").is_file()

    # `repos.dest_path` stayed NULL throughout: nothing back-filled the column to make the
    # assertion above true, so the destination really was computed on demand.
    rows = query(fleet, "SELECT dest_path FROM repos WHERE repo_id = 'acme-commons-java'")
    assert rows[0][0] is None, rows

    # …and Phase 4 resolves the SAME destination. §3.4 tests `//<dest>/...`, so a phase that
    # still read the NULL column would abandon every adapter-placed repo the moment the column
    # stopped being mandatory — a regression the build phase alone could not have shown.
    assert verify(fleet, "--rdeps-limit", "3").exit_code == ExitCode.SUCCESS
    tested = {
        pattern.removeprefix("//").removesuffix("/...")
        for call in bazel.calls
        for pattern in call.bazel
        if pattern.startswith("//") and pattern.endswith("/...")
    }
    assert {dests["acme-commons-java"], dests["acme-tool-py"]} <= tested, sorted(tested)


def test_module_bazel_carries_the_workspace_deps_the_adapters_declare(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
) -> None:
    """The root `MODULE.bazel` names every ruleset the fleet's adapters asked for and pins each
    at its `build.ruleset_versions` version — an empty set is no longer possible.

    **Why:** the degraded path declared no external dependencies at all, so `MODULE.bazel` was
    `module(name = "monorepo")` and nothing else for every fleet ever built. That file is the
    monorepo's whole external surface: an empty one is not a small inaccuracy, it is a monorepo
    in which nothing outside the tree can be depended on, and it rendered identically whether the
    reconciliation worked or never ran. The MVS-resolved version is asserted too — `33.2.1-jre`
    is what the pom declares and `33.2.1` is what §3.3 step 3 selects.

    **The pins are READ from `BuildSection`, never restated.** They were spelled out here as
    literals and it broke the moment the config legitimately moved: `aspect_rules_js` had to go
    2.1.3 → 3.4.0 because every 2.x floors a `rules_nodejs` whose `nodejs/toolchain.bzl` calls
    `rule(incompatible_use_toolchain_transition = …)`, which Bazel 9 removed — a correct fix that
    a hardcoded `"2.1.3"` turned into a red test, and would turn into a red test again at the next
    upgrade. A literal here is also a *weaker* assertion than it looks: it cannot fail when the
    renderer stops consulting the config at all, because both sides would still say 2.1.3. What
    this test owns is the claim "the version the operator configured is the version that lands in
    `MODULE.bazel`", which is exactly `pins[ruleset]` on both ends. `test_bazel.py` owns the
    orthogonal claim that those versions are ones real Bazel can load.
    """
    add_repos(fleet, ["acme-commons-java", "acme-ui-ts"])
    transformed(fleet)
    assert build(fleet, "--no-sandbox").exit_code == ExitCode.SUCCESS

    module = (build_worktree(fleet, "acme-commons-java") / "MODULE.bazel").read_text(
        encoding="utf-8"
    )
    pins = BuildSection().ruleset_versions
    for ruleset in ("rules_jvm_external", "aspect_rules_js", "rules_python"):
        line = f'bazel_dep(name = "{ruleset}", version = "{pins[ruleset]}")'
        assert line in module, f"{line}\nnot in:\n{module}"
    assert "maven.install(" in module, module
    assert '"33.2.1"' in module, module
    assert "npm.npm_translate_lock(" in module, module
    # One file for the whole monorepo: every repo's dispatch renders the identical superset, so
    # the second one to publish merges instead of conflicting on the integration branch.
    other = (build_worktree(fleet, "acme-ui-ts") / "MODULE.bazel").read_text(encoding="utf-8")
    assert other == module


def test_an_unknown_ecosystem_still_falls_back_visibly(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    filter_repo: FakeFilterRepo,
) -> None:
    """A repo no manifest adapter recognizes still gets the UNKNOWN adapter's single `filegroup`,
    still lands under that adapter's directory, and is still DISCLOSED as
    `EcosystemAdapterUnavailable` — while its siblings get real rules in the same run.

    **Why:** wiring the registry in must not close the §3.1 step 2 floor. A repo whose language
    nothing recognizes has to keep reaching the monorepo — sources merged and visible — and the
    reduction has to keep being reported. What changes is that the warning now means "this one
    repo", so it is worth reading; before, it fired for every repo in every run and therefore
    said nothing at all.
    """
    add_repos(fleet, ["acme-runbooks", "acme-ui-ts"])
    transformed(fleet)
    result = build(fleet, "--no-sandbox")
    assert result.exit_code == ExitCode.SUCCESS, result.output

    ecosystems.discover()
    assert repo_ecosystem(fleet, "acme-runbooks") is Ecosystem.UNKNOWN
    unknown = ecosystems.for_ecosystem(Ecosystem.UNKNOWN)
    dests = relocations(filter_repo)
    assert dests["acme-runbooks"] == f"{unknown.monorepo_dir}/acme-runbooks", dests

    body = (
        build_worktree(fleet, "acme-runbooks") / dests["acme-runbooks"] / "BUILD.bazel"
    ).read_text(encoding="utf-8")
    assert "filegroup(" in body, body
    assert "fleet_adapter=unknown" in body, body

    # Disclosed for that repo and ONLY that repo — the run's siblings got real rules.
    assert payload(result)["adapter_unavailable"] == ["acme-runbooks"], result.output
    rows = query(
        fleet, "SELECT repo_id FROM findings WHERE kind = 'EcosystemAdapterUnavailable'"
    )
    assert [str(row[0]) for row in rows] == ["acme-runbooks"], rows
    sibling = (
        build_worktree(fleet, "acme-ui-ts") / dests["acme-ui-ts"] / "BUILD.bazel"
    ).read_text(encoding="utf-8")
    assert "ts_project(" in sibling and "filegroup(" not in sibling, sibling


# ---------------------------------------------------------------------------------------
# §37 Blocker C — an ACTIVE stub redirects the consumer's generated dependency edge
# ---------------------------------------------------------------------------------------
#
# (Deliberately unnumbered: the "N." sections below this point are §7's real-binaries sequence,
# numbered 7-10, and this addition sits between sections 6 and 7 without renumbering either.)
#
# `_unit_deps` (cli.py) used to resolve every consumer -> provider graph edge to the provider's
# OWN internal Bazel label unconditionally — even when the provider is REQUIRES_HUMAN_INTERVENTION
# and a stub exists for it, which is precisely the case where that label was never merged onto the
# branch. These two tests seed a `stubs` row directly via SQL. **Corrected 2026-09-07 (round VI
# task 69 fix round): the original comment here said "no worker in this tree emits one yet --
# `--stub-blocked` is refused by `fleet build`" -- false since round VI task 69 removed all three
# `--stub-blocked` refusals and wired real stub creation into `_transform_impl`.** The raw-SQL
# seed stays here regardless: driving a real `--stub-blocked` TRANSFORM dispatch needs a genuinely
# abandoned provider and a real wave sequence (see `tests/test_pr_e2e.py::test_stub_blocked_
# creation_reaches_degraded_through_the_real_cli_and_feeds_t1_for_real` for that full real-CLI
# proof), which is out of scope for this file's own narrower target — `_unit_deps`'s redirect
# logic in isolation, over a stub row whose STATE is the one thing under test. These two tests
# prove BOTH halves of the fix on the ONE real internal edge that survives Phase 3
# (`acme-app-py` -> `acme-lib-py`, see `DEPENDENCY_REPO`/`DEPENDENT_REPO` below):
# an `ACTIVE` stub redirects the edge, and a `SUPERSEDED` one — the state T1 moves a stub to only
# once the provider's PR is `MERGED` (`orchestrator/stubs.py`'s `supersede`, ADR-0011 stacking) —
# does not, because by then the provider's real label IS live on the integration branch.

_STUB_PROVIDER: Final = "acme-lib-py"
_STUB_CONSUMER: Final = "acme-app-py"
#: The REAL `Coordinate.key` `acme-lib-py` publishes (`tests/test_scan_e2e.py`:
#: `published["pypi::acme-lib-py"] == "acme-lib-py"`) — `_unit_deps`'s stub lookup is keyed on
#: `edges.dst_coord_key`, so this has to be the genuine key, not a placeholder.
_STUB_COORD_KEY: Final = "pypi::acme-lib-py"
#: `bazel.layout.stub_dest("pypi::acme-lib-py")` fed through `cli._internal_label` — the same
#: recipe the (separately tracked, not-yet-built) stub-creation worker will use, so this is a
#: realistic `bazel_label`, not an arbitrary string.
_STUB_LABEL: Final = "//third_party/stubs/pypi__acme-lib-py:pypi__acme-lib-py"
#: The provider's own real label absent a stub — `cli._internal_label(DESTINATIONS["acme-lib-py"])`.
_PROVIDER_LABEL: Final = "//py/acme_lib_py:acme_lib_py"


def _insert_stub_row(
    root: Path,
    *,
    run_id: str,
    state: str,
    pinned_version: str | None = "2.0.1",
) -> None:
    """One `stubs` row for the `_STUB_CONSUMER` -> `_STUB_PROVIDER` edge, written straight to
    SQLite — the same reason (and shape) as `tests/test_pr_e2e.py`'s `degrade()`."""
    conn = sqlite3.connect(root / "state" / "fleet.db")
    try:
        conn.execute(
            "INSERT INTO stubs (stub_id, run_id, repo_id, stub_coord_key, consumer_repo_id, "
            "                   provider_repo_id, pinned_version, bazel_label, state, "
            "                   stub_fidelity, resolved_at, state_changed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PUBLISHED_ARTIFACT', ?, ?, ?)",
            (
                str(uuid4()),
                run_id,
                _STUB_CONSUMER,
                _STUB_COORD_KEY,
                _STUB_CONSUMER,
                _STUB_PROVIDER,
                pinned_version,
                _STUB_LABEL,
                state,
                # `schema.sql`'s CHECK requires `resolved_at` set on entry to any non-ACTIVE state.
                None if state == "ACTIVE" else "2026-08-09T00:00:00+00:00",
                "2026-08-09T00:00:00+00:00",
                "2026-08-09T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_an_active_stub_redirects_the_consumers_generated_dependency_to_the_stubs_label(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolver: FakeResolver,
) -> None:
    """`acme-lib-py` ends `REQUIRES_HUMAN_INTERVENTION`; an `ACTIVE` stub names it as the provider
    of `acme-app-py`'s real `acme-lib-py>=2.0` dependency. `acme-app-py`'s generated `py_library`
    must carry the STUB's label in `deps` — never `acme-lib-py`'s own, which was never merged.

    This is the fixture §37 Blocker C names: without the redirect, `_unit_deps` would still emit
    `//py/acme_lib_py:acme_lib_py` into `acme-app-py`'s `BUILD.bazel`, a label pointing at a
    package that Phase 3 never published (a failed repo's package never lands — see
    `test_a_build_failure_is_structured_and_does_not_take_its_siblings_down` above).

    **Why two `build()` calls.** §3.5's `blocked_by` propagation (`orchestrator/runner.py`'s
    `_contain`, unconditional — it does not consult `stubs`) marks `acme-app-py` `BLOCKED` the
    moment `acme-lib-py` reaches `REQUIRES_HUMAN_INTERVENTION`, in the SAME invocation, before its
    own wave ever opens: a `BLOCKED` member is never admitted (`scheduler.Admission`), so its
    `BUILD.bazel` is never written. **Corrected 2026-09-07 (round VI task 69 fix round):** this
    docstring used to say every `--stub-blocked` call site "still REFUSES as not-implemented" —
    false since round VI task 69 removed all three refusals. The real unblock path is now
    `fleet resume --stub-blocked` (§11.5 step 6, `orchestrator.reentry.stub_permits_removal` via
    `_unblock_dependents`); this test still uses the raw-SQL stand-in below rather than that real
    path because this file's fixtures do not drive `fleet resume`, and re-plumbing this test onto
    it is out of this fix round's scope — the raw-SQL write matches `clear_blocked_by`'s own
    effect (`BLOCKED -> PENDING` with `blocked_by` cleared) without exercising its floor/staleness
    machinery (`orchestrator/reentry.py`), which is landed and separately tested. The second
    `build()` call then admits `acme-app-py` through the ordinary, unmodified scheduler.

    **Corrected 2026-09-07 (round VI task 69 fix round): `acme-app-py`'s own BUILD row is now
    `DEGRADED`, not `SUCCEEDED`.** `cli._build_impl` now calls `stub_degrade_transform(phase=
    Phase.BUILD)` unconditionally after every wave (round VI task 69) — `models.state.RepoState.
    _stub_invariants` requires `stubbed_deps` non-empty IFF `status is DEGRADED`, so a repo built
    against a live `ACTIVE` stub must not project as plain `SUCCEEDED`. This is a real,
    intentional, production-visible behavior change this leg introduces: `fleet build` over any
    fleet carrying an `ACTIVE`/`PUBLISHED_ARTIFACT` stub row now leaves that consumer `DEGRADED`
    (and the overall run reports `REQUIRES_HUMAN_INTERVENTION`/exit 7) where it previously left it
    `SUCCEEDED`/exit 0 — unconditionally, with no flag needed, since the correction is data-driven
    off the `stubs` table rather than gated on `--stub-blocked`.
    """
    fake = FakeBazel(
        fleet / "artifacts" / "fake-bazel", fail={("build", DESTINATIONS[_STUB_PROVIDER]): 34}
    )
    monkeypatch.setattr(cli, "BAZEL_RUNNER", fake)
    filter_repo = FakeFilterRepo()
    monkeypatch.setattr(cli, "FILTER_REPO_RUNNER", filter_repo)

    transformed(fleet)
    run_id = str(query(fleet, "SELECT run_id FROM runs")[0][0])
    _insert_stub_row(fleet, run_id=run_id, state="ACTIVE")

    first = build(fleet, "--no-sandbox")
    assert first.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION, first.output
    statuses = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 3"))
    assert statuses[_STUB_PROVIDER] == "REQUIRES_HUMAN_INTERVENTION", statuses
    assert statuses[_STUB_CONSUMER] == "BLOCKED", statuses

    conn = sqlite3.connect(fleet / "state" / "fleet.db")
    try:
        conn.execute(
            "UPDATE phases SET status = 'PENDING', blocked_by = '[]' "
            " WHERE run_id = ? AND repo_id = ? AND phase = 3",
            (run_id, _STUB_CONSUMER),
        )
        conn.commit()
    finally:
        conn.close()

    result = build(fleet, "--no-sandbox")
    # Still REQUIRES_HUMAN_INTERVENTION overall: `acme-lib-py`'s own terminal failure is
    # re-reported every invocation (it never resolves), which is unrelated to whether THIS
    # invocation's own wave — `acme-app-py`'s — succeeded. That is checked below, directly.
    assert result.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION, result.output

    statuses = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 3"))
    assert statuses[_STUB_PROVIDER] == "REQUIRES_HUMAN_INTERVENTION", statuses
    # DEGRADED, not SUCCEEDED (round VI task 69 fix round) — see the docstring's second
    # "Corrected" paragraph above.
    assert statuses[_STUB_CONSUMER] == "DEGRADED", statuses

    dests = relocations(filter_repo)
    body = (
        build_worktree(fleet, _STUB_CONSUMER) / dests[_STUB_CONSUMER] / "BUILD.bazel"
    ).read_text(encoding="utf-8")
    assert f'"{_STUB_LABEL}"' in body, body
    assert f'"{_PROVIDER_LABEL}"' not in body, body


def test_a_superseded_stub_leaves_the_consumers_generated_dependency_on_the_real_label(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    filter_repo: FakeFilterRepo,
    resolver: FakeResolver,
) -> None:
    """The direct proof of the state predicate: a `SUPERSEDED` row for the SAME edge must NOT
    redirect it — `acme-app-py`'s generated `deps` must carry `acme-lib-py`'s own real label.

    `SUPERSEDED` means T1 already fired (`orchestrator/stubs.supersede`), which only happens once
    the provider's PR is `MERGED` (ADR-0011 stacking) — so the provider's real label is already
    live on the integration branch and `_unit_deps`'s ordinary, unmodified resolution is already
    correct here. Both repos build cleanly (the default `bazel` fixture), unlike the `ACTIVE`
    companion test above: this case is about the STATE column, not about a build failure.
    """
    _ = bazel
    transformed(fleet)
    run_id = str(query(fleet, "SELECT run_id FROM runs")[0][0])
    _insert_stub_row(fleet, run_id=run_id, state="SUPERSEDED")

    result = build(fleet, "--no-sandbox")
    assert result.exit_code == ExitCode.SUCCESS, result.output

    dests = relocations(filter_repo)
    body = (
        build_worktree(fleet, _STUB_CONSUMER) / dests[_STUB_CONSUMER] / "BUILD.bazel"
    ).read_text(encoding="utf-8")
    assert f'"{_PROVIDER_LABEL}"' in body, body
    assert f'"{_STUB_LABEL}"' not in body, body


# ---------------------------------------------------------------------------------------
# §37 Leg 2 (round VI task 68) — an ACTIVE, PUBLISHED_ARTIFACT stub's `workspace_deps()` render
# reaches the fleet's `MODULE.bazel` (`cli._stub_workspace_deps`/`_union_workspace_deps`)
# ---------------------------------------------------------------------------------------
#
# Deliberately a MAVEN-ecosystem coordinate (`acme-commons-java`, `com.acme:commons`), not one of
# the four lockfile-dialect ecosystems Blocker C's own fixture above uses: `maven.install` is the
# one dialect that pins a version PER ARTIFACT in the rendered tag itself (`ecosystems/jvm.py::
# workspace_deps`), so it is the one ecosystem where "the stub's pinned_version reached
# MODULE.bazel" is checkable by substring — the four others (py/js/rust/go) are version-free by
# dialect (the LOCK is the resolution) and never spell a per-coordinate version in the tag at
# all. No real consumer->provider edge is needed here (that redirect is Blocker C's job, already
# landed and out of scope for this leg) — `_stub_workspace_deps` reads only `stub_coord_key`/
# `pinned_version` off the `stubs` row.

_MAVEN_STUB_COORD_KEY: Final = "maven:com.acme:commons"
#: Deliberately DIFFERENT from `acme-commons-java`'s own real pom.xml version (`1.2.0`, see
#: `POLYGLOT_REPOS` above) — proving MODULE.bazel carries the STUB's `pinned_version`, not the
#: provider's own already-scanned `coordinates.version`.
_MAVEN_STUB_PINNED_VERSION: Final = "9.9.9"


def _insert_published_artifact_stub_row(
    root: Path,
    *,
    run_id: str,
    coord_key: str,
    provider_repo_id: str,
    consumer_repo_id: str,
    pinned_version: str,
) -> None:
    """One `ACTIVE`, `PUBLISHED_ARTIFACT` `stubs` row for an arbitrary coordinate, written
    straight to SQLite — same reason and shape as `_insert_stub_row` above (no worker in this
    tree emits one yet). Generalized over `coord_key`/`provider_repo_id` because this leg's
    render (`_stub_workspace_deps`) is exercised over a DIFFERENT ecosystem than Blocker C's own
    fixture (maven vs. pypi), and reads neither `bazel_label` nor `consumer_repo_id`.

    `bazel_label` is computed via `cli._internal_label(stub_dest(coord_key))` — the SAME function
    `cli._create_stub_records` uses for the real column, and `_STUB_LABEL` above computes by hand
    for the SAME reason (fix round, review finding M3: this helper previously hand-rolled a THIRD,
    inconsistent spelling — `f"...{coord_key.replace(':', '_')}:stub"` — matching neither).
    """
    conn = sqlite3.connect(root / "state" / "fleet.db")
    try:
        conn.execute(
            "INSERT INTO stubs (stub_id, run_id, repo_id, stub_coord_key, consumer_repo_id, "
            "                   provider_repo_id, pinned_version, bazel_label, state, "
            "                   stub_fidelity, resolved_at, state_changed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 'PUBLISHED_ARTIFACT', NULL, ?, ?)",
            (
                str(uuid4()),
                run_id,
                consumer_repo_id,
                coord_key,
                consumer_repo_id,
                provider_repo_id,
                pinned_version,
                cli._internal_label(stub_dest(coord_key)),
                "2026-09-07T00:00:00+00:00",
                "2026-09-07T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_an_active_published_artifact_stubs_workspace_dep_reaches_module_bazel(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    filter_repo: FakeFilterRepo,
    resolver: FakeResolver,
) -> None:
    """`fleet build`'s generated `MODULE.bazel` carries a `maven.install()` artifact for the
    stub's coordinate, pinned at the STUB's `pinned_version` — real render, through
    `ecosystems.for_ecosystem(...).workspace_deps()`, not a mock of it.

    `acme-commons-java` plays the abandoned PROVIDER here (its own real, scanned
    `com.acme:commons` coordinate is what the stub names); which repo plays the consumer is
    irrelevant to this leg's render (`_stub_workspace_deps` never reads `consumer_repo_id`), so
    the default fixture's `acme-app-py` is reused rather than adding a repo only to satisfy a
    schema NOT NULL column.
    """
    add_repos(fleet, ["acme-commons-java"])
    transformed(fleet)
    run_id = str(query(fleet, "SELECT run_id FROM runs")[0][0])
    _insert_published_artifact_stub_row(
        fleet,
        run_id=run_id,
        coord_key=_MAVEN_STUB_COORD_KEY,
        provider_repo_id="acme-commons-java",
        consumer_repo_id="acme-app-py",
        pinned_version=_MAVEN_STUB_PINNED_VERSION,
    )

    result = build(fleet, "--no-sandbox")
    # REQUIRES_HUMAN_INTERVENTION, not SUCCESS (round VI task 69 fix round): `acme-app-py` is
    # this stub's consumer and carries an ACTIVE/PUBLISHED_ARTIFACT row, so `cli._build_impl`'s
    # unconditional `stub_degrade_transform(phase=Phase.BUILD)` correction leaves its BUILD row
    # DEGRADED, forcing exit 7 — `models.state.RepoState._stub_invariants` requires this (a repo
    # built against a live ACTIVE stub must not project as plain SUCCEEDED). The render under
    # test (MODULE.bazel) is unaffected by this — it is written before the terminal status.
    assert result.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION, result.output
    consumer_status = dict(
        query(
            fleet,
            "SELECT repo_id, status FROM phases WHERE phase = 3 AND repo_id = ?",
            ("acme-app-py",),
        )
    )
    assert consumer_status["acme-app-py"] == "DEGRADED", consumer_status

    module = (build_worktree(fleet, "acme-commons-java") / "MODULE.bazel").read_text(
        encoding="utf-8"
    )
    assert "maven.install(" in module, module
    assert (
        f'name = "commons",\n    version = "{_MAVEN_STUB_PINNED_VERSION}",' in module
    ), module
    # The provider's OWN scanned version must not be what got rendered for this coordinate.
    assert '"1.2.0"' not in module, module


# ---------------------------------------------------------------------------------------
# §37 Leg 2 fix round (review finding I1) — the PACKAGE half of §3.5 item 1, complementing the
# test above (which only proves `_stub_workspace_deps`'s `MODULE.bazel` half). Without
# `cli._stub_package_files`, `_unit_deps`'s already-landed redirect names a label with no
# `BUILD.bazel` behind it anywhere on disk — invisible under `FakeBazel`, which never asks a real
# `bazel` to resolve anything. `tests/test_bazel.py`'s own real-subprocess tests prove the
# RESOLUTION half of this same mechanism; these two prove the WIRING half — that
# `_stub_package_files`'s rendered text actually lands in every dispatch's worktree at the exact
# path `stub_dest(coord_key)` names, for BOTH fidelities.
# ---------------------------------------------------------------------------------------


def _insert_empty_failing_stub_row(
    root: Path,
    *,
    run_id: str,
    coord_key: str,
    provider_repo_id: str,
    consumer_repo_id: str,
) -> None:
    """One `ACTIVE`, `EMPTY_FAILING` `stubs` row — `pinned_version` stays `NULL`, exactly as
    `schema.sql`'s own `CHECK (pinned_version IS NOT NULL OR stub_fidelity = 'EMPTY_FAILING')`
    requires. `bazel_label` via `cli._internal_label`, same as the PUBLISHED_ARTIFACT helper
    above, for the same M3 reason (one convention, never a hand-rolled second one).
    """
    conn = sqlite3.connect(root / "state" / "fleet.db")
    try:
        conn.execute(
            "INSERT INTO stubs (stub_id, run_id, repo_id, stub_coord_key, consumer_repo_id, "
            "                   provider_repo_id, pinned_version, bazel_label, state, "
            "                   stub_fidelity, resolved_at, state_changed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 'ACTIVE', 'EMPTY_FAILING', NULL, ?, ?)",
            (
                str(uuid4()),
                run_id,
                consumer_repo_id,
                coord_key,
                consumer_repo_id,
                provider_repo_id,
                cli._internal_label(stub_dest(coord_key)),
                "2026-09-07T00:00:00+00:00",
                "2026-09-07T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


_EMPTY_FAILING_STUB_COORD_KEY: Final = "npm::acme-abandoned-lib"


def test_active_stubs_package_files_are_materialized_into_every_dispatchs_worktree(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    filter_repo: FakeFilterRepo,
    resolver: FakeResolver,
) -> None:
    """`cli._stub_package_files`'s rendered `BUILD.bazel` for an `ACTIVE` stub of EITHER fidelity
    lands on disk at the exact `bazel.layout.stub_dest(coord_key)` path `cli._unit_deps`'s
    already-landed redirect names — one `PUBLISHED_ARTIFACT` row (the maven fixture above) and
    one `EMPTY_FAILING` row, seeded together, both materialized in the SAME `fleet build`.
    """
    add_repos(fleet, ["acme-commons-java"])
    transformed(fleet)
    run_id = str(query(fleet, "SELECT run_id FROM runs")[0][0])
    _insert_published_artifact_stub_row(
        fleet,
        run_id=run_id,
        coord_key=_MAVEN_STUB_COORD_KEY,
        provider_repo_id="acme-commons-java",
        consumer_repo_id="acme-app-py",
        pinned_version=_MAVEN_STUB_PINNED_VERSION,
    )
    _insert_empty_failing_stub_row(
        fleet,
        run_id=run_id,
        coord_key=_EMPTY_FAILING_STUB_COORD_KEY,
        provider_repo_id="acme-lib-py",
        consumer_repo_id="acme-app-py",
    )

    result = build(fleet, "--no-sandbox")
    # REQUIRES_HUMAN_INTERVENTION, not SUCCESS (round VI task 69 fix round) — same reason as
    # `test_an_active_published_artifact_stubs_workspace_dep_reaches_module_bazel` above:
    # `acme-app-py` is the consumer of the qualifying (PUBLISHED_ARTIFACT) stub row here too, so
    # its BUILD row is left DEGRADED by `stub_degrade_transform(phase=Phase.BUILD)`. The package
    # materialization under test is unaffected — it is written before the terminal status.
    assert result.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION, result.output

    worktree = build_worktree(fleet, "acme-commons-java")

    published_dest = stub_dest(_MAVEN_STUB_COORD_KEY)
    published_name = cli._internal_label(published_dest).rsplit(":", 1)[-1]
    published_body = (worktree / published_dest / "BUILD.bazel").read_text(encoding="utf-8")
    assert "alias(" in published_body, published_body
    assert (
        f'name = "{published_name}",\n    actual = "@maven//:commons",' in published_body
    ), published_body

    failing_dest = stub_dest(_EMPTY_FAILING_STUB_COORD_KEY)
    failing_name = cli._internal_label(failing_dest).rsplit(":", 1)[-1]
    failing_body = (worktree / failing_dest / "BUILD.bazel").read_text(encoding="utf-8")
    assert "genrule(" in failing_body, failing_body
    assert f'name = "{failing_name}",\n    cmd = ' in failing_body, failing_body
    assert "acme-lib-py" in failing_body, failing_body
    assert _EMPTY_FAILING_STUB_COORD_KEY in failing_body, failing_body


def _second_fleet_workspace(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A second, fully independent fleet workspace — its own temp root, sources, DB and
    monorepo — built the same way the `fleet` fixture builds one (`tests/test_transform_e2e.py`).

    Not a second call to the `fleet` fixture: that fixture is bound to one `tmp_path` per test, so
    the only way to run the pipeline twice inside one test with no shared state between the runs
    (no shared commits, no shared `phases` rows) is a second independent root. `--config`/`--db`
    are passed explicitly on every CLI invocation (`base_args`), but `scan`'s own `--repos`
    defaults to a CWD-relative `config/repos.yaml` and is refused if it disagrees with
    `--config`'s directory (`cli._validate_scan_flags`) — so this still needs its OWN `chdir`,
    same as the `fleet` fixture's. The caller is responsible for `chdir`-ing back to whichever
    workspace it invokes against next; `monkeypatch.chdir` only tracks the last call for teardown.
    """
    root = tmp_path_factory.mktemp("second-fleet")
    sources = {
        name: _make_repo(root / "sources", name, files) for name, files in FIXTURE_REPOS.items()
    }
    workspace = root / "workspace"
    workspace.mkdir()
    _write_config(workspace, sources, engine_module="fleet_fixture_engine")
    _write_engine(workspace, "fleet_fixture_engine", probe_available=True)
    _write_engine(workspace, "fleet_fixture_blind_engine", probe_available=False)
    write_rules(workspace, TS_IMPORT_RULE)
    _fresh_db(workspace / "state" / "fleet.db")
    monkeypatch.chdir(workspace)
    monkeypatch.syspath_prepend(str(workspace))
    importlib.invalidate_caches()
    make_monorepo(workspace)
    return workspace


_DEST_MARKER: Final = b"<DEST>"


def _tree_under(root: Path, dest: str) -> dict[str, bytes]:
    """Every regular file below `root`, keyed by its POSIX path relative to `root`, with every
    occurrence of `dest` in a file's own bytes replaced by a neutral marker.

    Rooting the walk AT a repo's own destination directory (`ts/acme/ui`, `vendored_ts/acme/ui`)
    rather than at the build worktree is what makes two such trees directly comparable: the
    overridden segment is the walk's root and never appears in a returned KEY, so a plain set
    comparison of the keys is already scoped to "everything but the overridden path segment(s)".

    The byte replacement is the same scoping applied to file CONTENT: a unit that references its
    own npm hub link (`//<dest>:node_modules/<pkg>`, ADR-0048) writes its own destination into its
    own `BUILD.bazel` — a legitimate difference the override exists to make, not drift. Only the
    exact `dest` string is masked, so any OTHER, unrelated difference still fails the comparison.
    """
    needle = dest.encode("utf-8")
    return {
        str(path.relative_to(root).as_posix()): path.read_bytes().replace(needle, _DEST_MARKER)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_a_monorepo_dir_override_really_moves_the_destination(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    filter_repo: FakeFilterRepo,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`build.monorepo_dir_overrides` moves an adapter-computed destination, and every artifact
    that names the destination moves with it — and produces the IDENTICAL tree §12 item 33
    claims, not merely a tree at a different path.

    **Why:** `EcosystemAdapter.monorepo_dir` is a `ClassVar` on a shared singleton, so §9's
    override has no way to reach it without a global mutation — which is exactly the shape of a
    "flag accepted, does nothing" defect this project has already fixed three times. The override
    is therefore applied in the driver, and this test is what stops it from being applied nowhere:
    the assertion is on the relocation argv and on the generated package, not on the config
    object that was read.

    **Why a second, independent fleet run:** "moved to `vendored_ts/acme/ui`" alone does not show
    the tree AT that destination is the same tree the default layout would have produced — a
    driver could apply the override and, say, drop `tsconfig` generation along some code path only
    the overridden branch takes, and every assertion above would still pass. A full SECOND
    `scan → sequence → transform → build` of the identical fixture repo, under the DEFAULT layout,
    gives a real `ts/acme/ui` tree to diff `vendored_ts/acme/ui` against.
    """
    ecosystems.discover()
    npm = repo_ecosystem(fleet, "acme-app-ts")  # never spelled: read back from the inventory

    baseline = _second_fleet_workspace(tmp_path_factory, monkeypatch)
    add_repos(baseline, ["acme-ui-ts"])
    transformed(baseline)
    assert build(baseline, "--no-sandbox").exit_code == ExitCode.SUCCESS
    baseline_dests = relocations(filter_repo)
    assert baseline_dests["acme-ui-ts"] == "ts/acme/ui", baseline_dests
    monkeypatch.chdir(fleet)

    config = fleet / "config" / "fleet.yaml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + f"build:\n  monorepo_dir_overrides:\n    {Ecosystem.NPM.value}: vendored_ts\n",
        encoding="utf-8",
    )
    add_repos(fleet, ["acme-ui-ts"])
    transformed(fleet)
    assert build(fleet, "--no-sandbox").exit_code == ExitCode.SUCCESS

    dests = relocations(filter_repo)
    adapter = ecosystems.for_ecosystem(npm)
    assert adapter.monorepo_dir != "vendored_ts", "the adapter itself must not be mutated"
    assert dests["acme-ui-ts"] == "vendored_ts/acme/ui", dests
    assert (
        build_worktree(fleet, "acme-ui-ts") / "vendored_ts/acme/ui" / "BUILD.bazel"
    ).is_file()
    # The repos that carry an explicit `dest:` are NOT moved: §9's override governs the directory
    # the adapter chose, and an operator who wrote a path is not overruled by it.
    assert dests["acme-app-ts"] == DESTINATIONS["acme-app-ts"], dests

    # The "identical tree" claim itself: everything the default layout put under `ts/acme/ui`
    # is, byte for byte, what the override put under `vendored_ts/acme/ui` — the destination
    # directory is the ONLY thing that differs, and it is the walk's root on both sides, so it
    # cannot appear inside either dict below.
    baseline_tree = _tree_under(build_worktree(baseline, "acme-ui-ts") / "ts/acme/ui", "ts/acme/ui")
    override_tree = _tree_under(
        build_worktree(fleet, "acme-ui-ts") / "vendored_ts/acme/ui", "vendored_ts/acme/ui"
    )
    assert baseline_tree, "the baseline tree must be non-empty or the comparison proves nothing"
    assert set(override_tree) == set(baseline_tree), (
        set(override_tree) ^ set(baseline_tree)
    )
    assert override_tree == baseline_tree


def test_an_override_colliding_with_another_adapters_directory_is_refused(
    fleet: Path,  # noqa: F811
) -> None:
    """An override aimed at a directory some *un-overridden* adapter still owns is refused at
    startup, by name, before anything is merged.

    **Why:** `BuildSection` already refuses two overrides pointing at one directory; it cannot see
    the collision with a directory nobody overrode. Two ecosystems under one root merge two
    languages' trees into one, and by the time Phase 3 has run there is no stage left that could
    separate them — so this has to fail loudly and early (Rule 11), not produce a tree.
    """
    ecosystems.discover()
    dirs = ecosystems.monorepo_dirs()
    config = fleet / "config" / "fleet.yaml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "build:\n  monorepo_dir_overrides:\n"
        + f"    {Ecosystem.NPM.value}: {dirs[Ecosystem.PYPI]}\n",
        encoding="utf-8",
    )
    scanned(fleet)
    result = transform(fleet, json_output=False)
    assert result.exit_code == ExitCode.USAGE, result.output
    assert "monorepo_dir_overrides" in result.output, result.output


# ---------------------------------------------------------------------------------------
# 6b. the lockfiles are RESOLVED — carry, resolve, or fail loudly; never an empty lock
# ---------------------------------------------------------------------------------------


def root_file(monorepo: Path, path: str) -> str:
    """One monorepo-root file as it exists ON the `integration` branch.

    Read out of git and not off a worktree, because the branch is what a later wave, a `fleet
    resume` and Phase 4 all build from — a lock that is correct in one dispatch's worktree and
    absent from the branch is the same defect as no lock at all.

    Not `git()`: that helper strips, and "the repo's own lockfile, unmodified" is a claim about
    bytes — a trailing newline dropped by the comparison is a comparison that would also pass if
    the harness had dropped it.
    """
    done = subprocess.run(  # noqa: S603
        ["git", "-C", str(monorepo), "show", f"integration:{path}"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout


def test_the_lockfiles_the_module_tags_name_are_resolved_not_synthesized(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    resolver: FakeResolver,
) -> None:
    """`fleet build` runs each adapter's declared resolver, in a scratch directory, over the
    inputs that adapter declared — and the resolver's OUTPUT is the lockfile that lands on the
    integration branch.

    **Why:** a lockfile is a resolution, and this harness had never run a resolver. The file
    `pip.parse(requirements_lock = "//:requirements.lock")` was handed was the *declared specs*
    Phase 1 recorded, which name no transitive dependency — so rules_python resolved `requests`,
    read its METADATA, asked its hub for `certifi`, and real Bazel reported `no such package
    '@@rules_python++pip+pypi//certifi'` about a distribution nothing in the fleet declares. The
    npm half was worse: a lock with no `packages:` section at all.

    Asserted on the resolver's ARGV and INPUTS rather than only on the file, because the file
    alone cannot tell "the resolver ran" from "the floor happened to be plausible": the scratch
    directory holds exactly what the driver wrote before the command ran, and an input the driver
    forgot is a resolver that resolved nothing while exiting 0.
    """
    transformed(fleet)
    assert build(fleet, "--no-sandbox").exit_code == ExitCode.SUCCESS

    assert sorted(resolver.tools()) == ["pnpm", "uv"], resolver.calls
    for call in resolver.calls:
        assert call.cwd is not None and "resolve" in call.cwd.parts, call.cwd
        # The resolver never runs in the tree Phase 3 is about to build: its inputs are a
        # SYNTHESIZED view of what stays external, and writing them into the worktree would put
        # files in the monorepo that no adapter declared and no MODULE.bazel names.
        assert "integration" not in call.cwd.parts, call.cwd
    written = {name: text for scratch in resolver.inputs for name, text in scratch.items()}
    assert "requests>=2.31" in written["requirements.in"], written["requirements.in"]
    # The ROOT manifest of a pnpm WORKSPACE holds no dependencies at all (ADR-0048). The flat
    # name-keyed map it replaces is the defect: unioned over the fleet it overwrote in place, so
    # two repos declaring one package at two specs emitted one specifier and lost the other with
    # no error anywhere.
    root_manifest = json.loads(written["package.json"])
    assert "dependencies" not in root_manifest, root_manifest
    assert root_manifest["private"] is True, root_manifest
    # …and the workspace names the importers, which is what makes them importers at all.
    assert "  - ts/acme/app\n" in written["pnpm-workspace.yaml"], written["pnpm-workspace.yaml"]
    assert "  - ts/acme/lib\n" in written["pnpm-workspace.yaml"], written["pnpm-workspace.yaml"]
    # THIS repo's own dependencies, in THIS repo's own importer manifest.
    manifest = json.loads(written["ts/acme/app/package.json"])
    assert manifest["dependencies"] == {
        "left-pad": "^1.3.0",
        "@acme/lib": "link:../lib",
    }, manifest
    # The INTERNAL sibling is in the manifest, and it is in it as a `link:` and NEVER as the
    # registry specifier its own `package.json` carries (`"@acme/lib": "^1.4.0"`). `@acme/lib` is
    # another repo of this fleet: asking pnpm to fetch it fails, or — far worse — succeeds
    # against an unrelated public package of that name. The `link:` is what puts a first-party
    # entry in the lock, which is what `npm_link_all_packages()` turns into
    # `//ts/acme/app:node_modules/@acme/lib` and what `tsc` finally resolves the import through.
    # It is IMPORTER-relative (`../lib`, not `ts/acme/lib`), because pnpm resolves `link:`
    # against the directory of the manifest that declares it — real pnpm 10.16.1 answers the
    # root-relative spelling with "Installing a dependency from a non-existent directory:
    # …/ts/acme/app/ts/acme/lib" and rules_js then joins it onto the importer path just the same.
    assert manifest["dependencies"]["@acme/lib"] == "link:../lib", manifest
    # ... and the sibling's own manifest is an input, because pnpm reads the manifest AT the
    # linked directory to learn the package's name; without it the resolver exits non-zero.
    assert json.loads(written["ts/acme/lib/package.json"])["name"] == "@acme/lib", written

    assert root_file(monorepo, "requirements.lock") == FAKE_LOCKS["uv"][1]
    assert root_file(monorepo, "pnpm-lock.yaml") == FAKE_LOCKS["pnpm"][1]


class WritebackBazel(FakeBazel):
    """A Bazel that EDITS THE WORKTREE it was pointed at, which real ones do.

    Not a hypothetical: `crate_universe`'s `crate.from_cargo` rewrites `//:Cargo.lock` in place
    while the build runs — `skip_cargo_lockfile_overwrite` defaults False — so between the step
    that materializes the planned support files and the step that commits them, the tree has
    changed under the harness. This stands in for that with one line of damage to a root support
    file, because what is under test is the publish contract and not cargo.
    """

    def __init__(self, log_root: Path, *, paths: Sequence[str]) -> None:
        super().__init__(log_root)
        self.paths = tuple(paths)
        #: `(worktree name, path)` for every file this fake really damaged.
        self.mutated: set[tuple[str, str]] = set()

    async def __call__(self, argv: Sequence[str], **kwargs: Any) -> ProcResult:
        cwd = kwargs.get("cwd")
        if cwd is not None and Invocation(tuple(argv), cwd).bazel[1] == "build":
            for path in self.paths:
                target = Path(cwd) / path
                if target.is_file():
                    target.write_text(
                        target.read_text(encoding="utf-8") + WRITEBACK_LINE, encoding="utf-8"
                    )
                    self.mutated.add((Path(cwd).name, path))
        return await super().__call__(argv, **kwargs)


WRITEBACK_LINE = "\n# written by the build system, not by any plan\n"


def publish_commit(monorepo: Path, dest: str) -> str:
    """The single Phase 3 publish commit for one `dest`, found by its `Fleet-Repo-Id` trailer.

    The merge commit that carries it onto `integration` wears no such trailer (§3.3 step 1 keeps
    ADR-0011's provenance pair off both), so this names the commit whose *tree* the publish step
    actually built — which is the object this assertion is about. Reading the branch tip instead
    would let a later repo's correct publish paper over an earlier one's wrong one.
    """
    fmt = "%H%x1f%(trailers:key=Fleet-Repo-Id,valueonly)%x1f%s%x1e"
    raw = git(monorepo, "log", f"--format={fmt}", "integration")
    found: list[str] = []
    for record in raw.split("\x1e"):
        fields = record.strip("\n").split("\x1f")
        if len(fields) != 3:
            continue
        sha, repo_id, subject = (field.strip() for field in fields)
        if repo_id and subject == f"Generate Bazel targets for {dest}":
            found.append(sha)
    assert len(found) == 1, (dest, found, raw)
    return found[0]


def blob(monorepo: Path, sha: str, path: str) -> str:
    """`git show <sha>:<path>`, unstripped — this is a claim about bytes."""
    done = subprocess.run(  # noqa: S603
        ["git", "-C", str(monorepo), "show", f"{sha}:{path}"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout


def test_publish_commits_the_planned_support_file_bytes_not_what_the_build_left_on_disk(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    monkeypatch: pytest.MonkeyPatch,
    filter_repo: FakeFilterRepo,
    resolver: FakeResolver,
) -> None:
    """A build that rewrites a tracked root support file in the worktree publishes the PLANNED
    bytes anyway — `git show <published_sha>:<path>` is `SupportFile.content`, byte for byte.

    **The defect.** GENERATE materializes every support file from `SupportFile.content`, VERIFY
    then runs Bazel with `cwd=` that same worktree, and PUBLISH used to `git add` whatever was on
    disk by then. `crate_universe` rewrites `//:Cargo.lock` as it builds, so the one Rust repo
    whose build ran green published a cargo-extended lock while every sibling published the
    materialized plan, and the next merge was `CONFLICT (add/add): Merge conflict in Cargo.lock`.
    §11.6 byte-determinism, broken by the build system editing the tree rather than by any
    nondeterministic generator — and invisible to `is_dirty()`, which read the writeback as real
    work to publish.

    **Why this asserts the invariant and not the symptom.** The tempting assertion is that the
    merges no longer conflict, and it is worthless: it goes green on a change of merge ORDER,
    which fixes nothing and unfixes itself on the next wave. The property that has to hold is
    per-commit and order-free — every repo commits the plan — and it is asserted on the publish
    commit's own tree, not the branch tip, so a later repo's correct publish cannot cover for an
    earlier repo's wrong one.

    Asserted through the npm and PyPI root locks rather than Cargo's because the fix is a
    publish-contract rule, not a Rust workaround: no ecosystem's build output may become a commit,
    whether or not that ecosystem writes back today. Two ecosystems, so a fix that re-asserted one
    adapter's files would fail here. `mutated` is checked so a fake that quietly stopped writing
    would fail this test instead of passing an assertion about a defect it never staged.
    """
    _ = (filter_repo, resolver)
    # The repo that OWNS each root lock — the one whose plan declares it as a `SupportFile`, i.e.
    # the unit that contributes external coordinates to that ecosystem.
    damaged = {
        "acme-app-ts": ("pnpm-lock.yaml", FAKE_LOCKS["pnpm"][1]),
        "acme-app-py": ("requirements.lock", FAKE_LOCKS["uv"][1]),
    }
    fake = WritebackBazel(
        fleet / "artifacts" / "fake-bazel", paths=[path for path, _ in damaged.values()]
    )
    monkeypatch.setattr(cli, "BAZEL_RUNNER", fake)

    transformed(fleet)
    assert build(fleet, "--no-sandbox").exit_code == ExitCode.SUCCESS

    assert fake.mutated >= {(repo, path) for repo, (path, _) in damaged.items()}, (
        fake.mutated,
        "the fake did not actually stage the defect, so a pass proves nothing",
    )
    for repo_id, (path, planned) in damaged.items():
        sha = publish_commit(monorepo, DESTINATIONS[repo_id])
        assert blob(monorepo, sha, path) == planned, (repo_id, path)
        # …and on the branch a later wave, `fleet resume` and Phase 4 all build from.
        assert root_file(monorepo, path) == planned, path


def test_a_repo_that_ships_its_own_lock_keeps_it_and_no_resolver_runs(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    resolver: FakeResolver,
) -> None:
    """A `requirements.lock` the source repo really shipped reaches the monorepo root byte for
    byte, and the resolver is **not invoked for it**.

    **Why:** re-resolving a lock a repo pinned is not a neutral act. The repo tested against those
    versions; a resolution computed today picks whatever the index publishes today, and the
    migration would silently move every one of them while looking entirely normal in review. So
    the precedence is carry → resolve → floor, and the second half of that sentence is what this
    asserts: not merely that the bytes survived, but that no resolver ran to produce them.

    The npm resolver still runs, for the repo that ships no lock — which is what makes the first
    half a statement about *this* repo rather than about a run in which resolution was switched
    off entirely.
    """
    shipped = "requests==2.31.0\ncertifi==2024.2.2\n    # via requests\n"
    source = fleet.parent / "sources" / "acme-app-py"
    (source / "requirements.lock").write_text(shipped, encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-m", "ship a real resolution")

    transformed(fleet)
    assert build(fleet, "--no-sandbox").exit_code == ExitCode.SUCCESS

    assert root_file(monorepo, "requirements.lock") == shipped, "the repo's own resolution"
    assert resolver.tools() == ["pnpm"], "a shipped lock must not be re-resolved"


def test_a_second_python_repo_revokes_the_carry_and_forces_a_union_resolve(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    resolver: FakeResolver,
) -> None:
    """A shipped `requirements.lock` stops being carried the moment a SECOND Python repo
    contributes external dependencies — the root lock is resolved over the union instead.

    **The defect this pins.** `//:requirements.lock` is ONE file that `pip.parse` turns into ONE
    `@pypi` hub for every Python package in the monorepo. With two contributing units the
    adapter's fleet-wide `workspace_files()` still offered a `carry_from` — and it offered
    exactly one unit's, the lexicographically first `dest`. `cli._carried` then short-circuited
    the resolver entirely and `acme-metrics-py`'s single-package lock was promoted to the whole
    fleet's, so `requests` was absent from the hub `//py/acme_app_py:acme_app_py` resolves
    `@pypi//requests` in. Nothing raised: `_fleet_support_files` hands every plan of an ecosystem
    the identical tuple, so `_module_inputs`' `RootFileConflictError` never sees a divergence to
    report, and which repo won was a function of string ordering (`py/acme-metrics-py` sorts
    before `py/acme_app_py` because `-` < `_`).

    **Why revoking the carry is the fix and not raising.** Two Python repos in one monorepo is
    the normal case, not an error, so raising would make the fleet unbuildable for doing nothing
    wrong. The root lock under ≥2 contributors is a resolution of the UNION, which no single
    repo's lock is — the same reasoning ADR-0048 applies to the pnpm workspace lock, and the same
    `carry_from` → `Resolution` → `content` precedence in `models/build.py` selecting its middle
    term. A genuine version conflict between the two repos is then a loud `uv` failure naming
    both specs (`test_contradictory_specs_from_two_python_repos_reach_the_resolver_unmerged`
    covers the input side offline), which is the honest outcome; a silent winner is not.

    The single-contributor carry is NOT weakened — that is
    `test_a_repo_that_ships_its_own_lock_keeps_it_and_no_resolver_runs` above, and the two
    together are the whole rule.
    """
    shipped = "jinja2==3.1.4\nmarkupsafe==2.1.5\n    # via jinja2\n"
    add_repos(fleet, ["acme-metrics-py"])
    source = fleet.parent / "sources" / "acme-metrics-py"
    (source / "requirements.lock").write_text(shipped, encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-m", "ship a real resolution")

    transformed(fleet)
    assert build(fleet, "--no-sandbox").exit_code == ExitCode.SUCCESS

    assert "uv" in resolver.tools(), (
        "with two contributing Python units the root lock is the union's resolution, so the "
        "resolver must run; a carry here skips it entirely"
    )
    written = {name: text for scratch in resolver.inputs for name, text in scratch.items()}
    # BOTH repos' specs reached the resolver's input. This is the assertion the carry made
    # unreachable: the short-circuit returned before `requirements.in` was ever written.
    assert "requests>=2.31" in written["requirements.in"], written["requirements.in"]
    assert "jinja2>=3.1" in written["requirements.in"], written["requirements.in"]
    landed = root_file(monorepo, "requirements.lock")
    assert landed == FAKE_LOCKS["uv"][1], landed
    assert landed != shipped, (
        "one repo's single-package lock promoted to the monorepo root describes a fleet that "
        "does not exist, and drops every distribution the other repo declared"
    )


def test_the_go_root_sum_is_resolved_from_the_union_go_mod_and_never_carried_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Go's `Resolution` through the REAL driver — argv, inputs, the carry that must NOT happen,
    and the bytes the materializer puts at the monorepo root (ADR-0050).

    **What was missing.** `go_deps.bzl` calls `sums_from_go_mod` whenever the `go.mod` it loads
    carries any `require`, and that function reads a `go.sum` sitting beside it. The root
    `go.mod` has carried `require` lines since `go.py` was written and **no phase had ever
    produced the sums** — `go.sum` had zero occurrences anywhere in `src/`. Nothing in this
    project could see the gap, because a `go.sum` is named by no label: it is found by adjacency,
    so the D10 check that walks `//:` references out of the generated text has nothing to walk.

    **Driven through `cli._resolved_support_files` rather than through a Go repo in the fixture
    fleet, and that boundary is the honest one.** This asserts the PLUMBING: the command, the
    file the driver staged before running it, the file it did not carry, and where the answer
    lands. It asserts nothing about whether the sums are correct, whether `go_deps.from_file`
    accepts them, or whether any Go code compiles: a real resolve costs a live
    `proxy.golang.org` fetch, which is exactly what the seam exists to keep out of this suite.

    **The decoy `go.sum` in the source tree is the load-bearing half.** `cli._carried`
    short-circuits the resolver for any support file with a real candidate behind it, so an
    adapter that spelled `carry_from=["<dest>/go.sum"]` would promote this repo's sums to the
    monorepo root and never run `go mod download all` at all. Those sums are hashes taken against
    the repo's OWN `go.mod`; beside a fleet-owned root `go.mod` they are missing entries for what
    it requires and stale for what it does not, which surfaces inside Bazel as a checksum
    mismatch — a failure that reads as a supply-chain compromise and that this harness would have
    manufactured itself.

    **The `go.mod` is the fleet's UNION and not the repo's own (ADR-0050 step 2), which is the
    other decoy here.** The source tree below ships a real `go.mod`, and it stays at
    `<dest>/go.mod` where Phase 2 left it: what the resolver reads and what lands at the root is
    the monorepo's own module over every Go unit's requirements. Asserting the landed bytes are
    NOT the shipped ones is what would catch a `carry_from` being restored on that file — which
    would look harmless, and would silently skip `go mod download all` via `cli._carried` while
    putting one repo's module declaration at the root of a monorepo.
    """
    ecosystems.discover()
    adapter = ecosystems.for_ecosystem(Ecosystem.GO)
    unit = BuildUnit(
        unit_id="acme-commons-go",
        ecosystem=Ecosystem.GO,
        dest="go/commons",
        srcs=["commons.go"],
        published=Coordinate(ecosystem=Ecosystem.GO, group="github.com/acme", name="commons"),
        external_coordinates=[
            Coordinate(
                ecosystem=Ecosystem.GO,
                group="github.com/stretchr",
                name="testify",
                version_spec="v1.9.0",
            )
        ],
    )
    worktree = tmp_path / "worktree"
    package = worktree / unit.dest
    package.mkdir(parents=True)
    shipped_mod = (
        "module github.com/acme/commons\n\ngo 1.23.4\n\n"
        "require github.com/stretchr/testify v1.9.0\n"
    )
    (package / "go.mod").write_text(shipped_mod, encoding="utf-8")
    decoy = "github.com/acme/stale v0.1.0 h1:this-repos-own-sums-are-not-the-fleets=\n"
    (package / "go.sum").write_text(decoy, encoding="utf-8")

    resolver = FakeResolver()
    monkeypatch.setattr(cli, "RESOLVER_RUNNER", resolver)
    resolved = asyncio.run(
        cli._resolved_support_files(
            adapter,
            [unit],
            repo_id=str(unit.unit_id),
            worktrees=[worktree],
            scratch=tmp_path / "scratch",
        )
    )

    (call,) = resolver.calls
    assert list(call.argv) == ["go", "mod", "download", "all"], (
        "`go mod tidy` deletes the require block of the very file it is resolving and writes no "
        "go.sum; bare `go mod download` writes only the /go.mod hashes and no `h1:` zip hash"
    )
    # The scratch tree the command actually ran against, recorded BEFORE it ran — the resolver
    # having a `go.mod` to read is the difference between a resolution and an exit-0 no-op.
    (staged,) = resolver.inputs
    assert set(staged) == {"go.mod"}, staged
    assert "module fleet.internal/monorepo" in staged["go.mod"], staged["go.mod"]
    assert "\tgithub.com/stretchr/testify v1.9.0" in staged["go.mod"], staged["go.mod"]
    assert staged["go.mod"] != shipped_mod, (
        "the sums must hash the file that LANDS at the root; resolving one repo's go.mod beside "
        "a fleet-owned one is a checksum mismatch in which every individual hash is correct"
    )

    files = {support.path: support.content for support in resolved}
    assert set(files) == {"go.mod", "go.sum"}
    assert files["go.sum"] == FAKE_LOCKS["go"][1]
    assert files["go.sum"] != decoy, "a carried go.sum is a checksum mismatch waiting to happen"
    assert files["go.mod"] == staged["go.mod"], (
        "the resolver read one go.mod and a different one landed at the root"
    )
    assert files["go.mod"] != shipped_mod, (
        "a carry_from restored on the root go.mod would promote one repo's module declaration to "
        "the monorepo root AND skip the resolver entirely through `cli._carried`"
    )
    assert (worktree / unit.dest / "go.mod").read_text(encoding="utf-8") == shipped_mod, (
        "the repo's own go.mod is not lost; Phase 2 left it at <dest>/go.mod"
    )

    # …and the bytes reach the monorepo root, through the shipped materializer that Phase 3
    # step 2 runs. `go.sum` is at the ROOT and not under `<dest>/`, because that is where the
    # `go.mod` `go_deps.from_file` names sits and adjacency is the whole of how Go finds it.
    root = tmp_path / "monorepo"
    root.mkdir()
    assert materialize(root, resolved) == ["go.mod", "go.sum"]
    assert (root / "go.sum").read_text(encoding="utf-8") == FAKE_LOCKS["go"][1]


#: The two Go fixtures' destinations. `go/<module tail>` is the `GoAdapter`'s own answer
#: (`monorepo_dir` + `path_tail`, which drops the host and org), restated here only so the
#: assertions below can be read — every test asserts it against `git-filter-repo`'s real argv.
GO_DESTS: dict[str, str] = {"acme-clitool-go": "go/clitool", "acme-digest-go": "go/digest"}

#: Every `require` line the fleet's root `go.mod` must carry with BOTH Go fixtures in, exactly as
#: `go.py:_go_requires` renders them (a leading tab, `<module path> <version>`), sorted.
#:
#: The first three come from `acme-clitool-go` and the last two from `acme-digest-go`, and the
#: split is the whole point: this is the ADR-0050 step 2 union, so a root file holding only one
#: contiguous half of this tuple is a root file that dropped a repo. The `// indirect` entries are
#: here because `manifests/gomod.py` keeps them deliberately — Go's MVS puts the whole transitive
#: closure in `go.mod`, and an indirect requirement is still an edge.
GO_UNION_REQUIRES: tuple[str, ...] = (
    "\tgithub.com/inconshreveable/mousetrap v1.1.0",
    "\tgithub.com/spf13/cobra v1.8.1",
    "\tgithub.com/spf13/pflag v1.0.5",
    "\tgolang.org/x/crypto v0.31.0",
    "\tgolang.org/x/sys v0.28.0",
)

#: The monorepo's root `go.mod`, verbatim, with both Go fixtures in the fleet — the bytes
#: `go_deps.from_file(go_mod = "//:go.mod")` reads and `go mod download all` hashes.
#:
#: There is no trailing newline, and that is not a typo: `SupportFile` is a `FleetModel`, whose
#: `model_config` sets `str_strip_whitespace=True`, so every support file's `content` reaches disk
#: stripped. `go.py:_go_mod_text` does end its render with `\n`. That is a property of the model
#: and of every ecosystem's support files alike, not of Go, and it is pinned here rather than
#: papered over with an `rstrip` so that a change to it is visible instead of silent.
GO_ROOT_GO_MOD: str = (
    "// GENERATED BY fleet — the monorepo's own module over every Go repo's requirements.\n"
    "module fleet.internal/monorepo\n"
    "\n"
    "go 1.24.12\n"
    "\n"
    "require (\n" + "".join(f"{line}\n" for line in GO_UNION_REQUIRES) + ")"
)


def _go_units_from_fixture_manifests() -> dict[str, BuildUnit]:
    """A `BuildUnit` per Go fixture, with its coordinates parsed out of the fixture's own `go.mod`
    by the SHIPPED `manifests/gomod.py` adapter.

    Parsed rather than restated, because a hand-written coordinate list would make the union tests
    below assert against their own input: the claim is about what the fleet's `go.mod` files mean,
    and `GomodAdapter.parse`/`.coordinate`/`.publishes` is the only thing entitled to say.
    """
    manifests.discover()
    adapter = manifests.adapter_for(Path("go.mod"))
    assert adapter is not None
    out: dict[str, BuildUnit] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for repo_id, dest in GO_DESTS.items():
            path = Path(tmp) / repo_id / "go.mod"
            path.parent.mkdir(parents=True)
            path.write_text(POLYGLOT_REPOS[repo_id]["go.mod"], encoding="utf-8")
            out[repo_id] = BuildUnit(
                unit_id=repo_id,
                ecosystem=Ecosystem.GO,
                dest=dest,
                srcs=[],
                published=adapter.publishes(path),
                external_coordinates=[adapter.coordinate(raw) for raw in adapter.parse(path)],
            )
    return out


def test_two_go_repos_relocate_by_the_adapters_layout_and_union_into_one_root_go_mod(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    resolver: FakeResolver,
    filter_repo: FakeFilterRepo,
) -> None:
    """Two Go repos through the real CLI: the destinations `layout()` computed, the ONE root
    `go.mod` that is the union of BOTH repos' requirements, and the `go.sum` resolved beside it.

    **The first time a Go REPOSITORY reaches Phase 3 in this suite.** Every other Go assertion in
    this file hand-builds a `BuildUnit`, so `manifests/gomod.py`'s parse, `GoAdapter.layout()` and
    §3.3 step 2's fleet-wide union had never met a `go.mod` that came out of a scan — and the
    union in particular is unobservable with one Go repo, which is why the fixture is a pair.

    **What is asserted, and each one is a thing that could be wrong.**

    1. *The destinations are the ADAPTER's.* Neither fixture declares a `dest:`, so `go/clitool`
       and `go/digest` are `monorepo_dir` + `path_tail` dropping the host and org, read off
       `git-filter-repo`'s own `--path-rename` argv rather than off a column that is NULL for
       exactly the adapter-computed case this covers.
    2. *One wave.* Neither fixture declares an internal dependency, so §3.1 step 7 puts both in
       one wave — asserted, because a fixture that split across waves would compute the root
       `go.mod` over one repo at a time and this test would be about something else entirely.
    3. *The root `go.mod` is the UNION, and it is the fleet's own module.* Every one of
       `GO_UNION_REQUIRES` is on the branch, `github.com/spf13/cobra` (only `acme-clitool-go`
       declares it) and `golang.org/x/crypto` (only `acme-digest-go` does) both among them. That
       is ADR-0050 step 2's defect stated positively: `go/digest` sorts second, so a
       first-writer-wins `setdefault` over the fleet's root files drops precisely `x/crypto` and
       `x/sys` while `fleet build` still exits 0.
    4. *Neither repo's own `go.mod` is at the root, and neither is lost.* The root declares
       `module fleet.internal/monorepo`; each repo's own file is still at `<dest>/go.mod`, where
       Phase 2 left it.
    5. *The `go.sum` is RESOLVED and never carried.* Both fixtures ship a real `go.sum` — hashes
       taken against their own `go.mod` — and both are decoys: `cli._carried` short-circuits the
       resolver for any support file with a candidate behind it, so a `carry_from` on this file
       would promote one repo's sums to the root beside a unioned `go.mod` and never run the
       resolver at all. The command is `go mod download all`, its staged input is byte-identical
       to the `go.mod` that lands, and the answer on the branch is the resolver's.

    **The resolver is `FakeResolver`, and that boundary is deliberate.** A real `go mod download
    all` is a live `proxy.golang.org` fetch; what this test is about is the plumbing — which file
    the driver staged, which command it ran over it, and which bytes reached the branch — and that
    is entirely the harness's own code. The fixtures' `go.sum`s are the real thing (generated by
    `tools/bin/go mod download all`), so the decoy this refuses to carry is a genuine one.

    **What this does NOT prove.** No Go was compiled and no Bazel looked at any of it. The
    BUILD-file generator did run in this fleet, but through the `cli.GAZELLE_RUNNER` seam — no
    real Gazelle produced anything here, and nothing in this test asserts about its output;
    `test_the_generated_go_build_files_carry_the_targets_the_generator_produced` below is where
    the captured targets are asserted, under the same disclosure.
    """
    add_repos(fleet, list(GO_DESTS))
    transformed(fleet)
    result = build(fleet, "--no-sandbox")
    assert result.exit_code == ExitCode.SUCCESS, result.output

    dests = relocations(filter_repo)
    assert {repo: dests.get(repo) for repo in GO_DESTS} == GO_DESTS, (
        "`go/<module tail>` is the GoAdapter's own layout answer and neither fixture declares a "
        f"`dest:`; got {dests}"
    )
    waves = {repo_id: wave_index(fleet, repo_id) for repo_id in GO_DESTS}
    assert len(set(waves.values())) == 1, (
        "the two Go repos landed in different waves, so the fleet-wide root `go.mod` was never "
        f"computed over both at once and the union below would assert nothing: {waves}"
    )

    landed = root_file(monorepo, "go.mod")
    assert "module fleet.internal/monorepo" in landed, landed
    missing = [line for line in GO_UNION_REQUIRES if f"{line}\n" not in landed]
    assert not missing, (
        "the monorepo's one `//:go.mod` does not carry every Go repo's requirements — "
        f"{missing} are declared by a repo that is merged onto `integration`, and "
        f"`go_deps.from_file` reads this file and nothing else:\n{landed}"
    )
    assert landed == GO_ROOT_GO_MOD, landed
    for repo_id, dest in GO_DESTS.items():
        own = POLYGLOT_REPOS[repo_id]["go.mod"]
        assert landed != own, (
            f"the root `go.mod` is {repo_id}'s own file: a `carry_from` here promotes one repo's "
            "module declaration to the monorepo root AND skips the resolver via `cli._carried`"
        )
        assert root_file(monorepo, f"{dest}/go.mod") == own, (
            f"{repo_id}'s own go.mod is not at {dest}/go.mod, where Phase 2 left it"
        )

    assert ["go", "mod", "download", "all"] in [list(call.argv) for call in resolver.calls], (
        "the root `go.sum` was not resolved at all; `go.py:_RESOLVER` is the one command that "
        f"writes both hash kinds, and the resolvers that ran were {resolver.tools()}"
    )
    staged = [scratch for scratch in resolver.inputs if set(scratch) == {"go.mod"}]
    assert staged, f"the Go resolver was run over a scratch with no go.mod in it: {resolver.inputs}"
    assert staged[-1]["go.mod"] == landed, (
        "the sums hash the go.mod the resolver READ, so a different go.mod landing at the root is "
        "a checksum mismatch in which every individual hash is correct"
    )
    sums = root_file(monorepo, "go.sum")
    assert sums == FAKE_LOCKS["go"][1], sums
    for repo_id in GO_DESTS:
        assert sums != POLYGLOT_REPOS[repo_id]["go.sum"], (
            f"{repo_id}'s own go.sum was promoted to the monorepo root; those hashes are taken "
            "against ITS go.mod and are missing entries for every other Go repo's modules"
        )
    _ = bazel


def test_the_root_go_mod_union_is_the_same_bytes_in_either_repo_order(
    tmp_path: Path,
) -> None:
    """`workspace_files()` over the two Go fixtures renders IDENTICAL bytes in both unit orders.

    **Why order is the thing worth pinning.** The union is fed by `cli._fleet_support_files`, which
    groups the plans the driver has accumulated so far — a dict whose order is the order repos
    settled in, which is a function of wave membership, scheduling and how fast each dispatch
    returned. §11.6 requires one plan to render one set of bytes in every process, and a root
    `go.mod` whose `require` block is emitted in arrival order would instead produce a different
    file — and therefore, through `go mod download all`, a different `go.sum` — depending on which
    Go repo's build finished first. Nothing else in this suite would notice: both orderings name
    the same modules, so every "is `x/crypto` in the union" assertion stays green.

    Stated over BOTH directions of a two-element list and over the exact union tuple, so it fails
    for an unsorted render and also for a render that sorted but dropped or duplicated a line. The
    coordinates come from `manifests/gomod.py` parsing the fixtures' own `go.mod` text, so this is
    a claim about the fixtures rather than about a list retyped into the test.

    **What this does NOT prove.** Nothing about the resolver, Bazel, or whether the union is
    *correct* — only that it is a function of the fleet and not of the order the driver saw it in.
    """
    _ = tmp_path
    ecosystems.discover()
    adapter = ecosystems.for_ecosystem(Ecosystem.GO)
    units = _go_units_from_fixture_manifests()
    clitool, digest = units["acme-clitool-go"], units["acme-digest-go"]

    forward = {file.path: file.content for file in adapter.workspace_files([clitool, digest])}
    reverse = {file.path: file.content for file in adapter.workspace_files([digest, clitool])}
    assert forward == reverse, (
        "the fleet's root `go.mod` depends on the order the driver grouped the Go units in, so "
        "one plan renders two files across two processes (§11.6) — and two `go.sum`s under them"
    )
    assert set(forward) == {"go.mod", "go.sum"}, forward
    body = forward["go.mod"]
    assert body == GO_ROOT_GO_MOD, body
    # Both halves, named individually: a union that kept one repo's block would still be sorted.
    assert "\tgithub.com/spf13/cobra v1.8.1" in body, body
    assert "\tgolang.org/x/crypto v0.31.0" in body, body
    # …and the resolver reads the SAME bytes, from the same two pure functions — not a second
    # rendering that happens to agree today.
    for order in ([clitool, digest], [digest, clitool]):
        plan = adapter.resolution(order)
        assert plan is not None
        assert [(item.path, item.content) for item in plan.inputs] == [("go.mod", body)], plan


def _generated(monorepo, path: str) -> str:
    """One generated BUILD file as it exists ON the `integration` branch.

    `root_file` under another name and used for package files rather than root ones, because the
    claim these tests make is about a path at arbitrary depth — `go/clitool/cmd/clitool/BUILD.bazel`
    is three levels below the `dest` the publish step used to stage by literal name.
    """
    return root_file(monorepo, path)


def test_the_build_file_generator_runs_once_over_every_go_root_with_static_resolution(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    gazelle: FakeGazelle,
) -> None:
    """ONE invocation, over EVERY Go repo root, with `-external=static` and `-index=all`, in a
    scratch tree that is not any build worktree.

    **Every clause is load-bearing and each was measured against the vendored binary.**

    1. *One invocation covering both roots.* With both roots on the command line an import of a
       sibling resolves to a real in-repo label (`//go/digest`); with only one root passed the
       same import is **silently dropped and the generator exits 0**. Cross-repo edges are the
       entire point of a monorepo migration, so a per-repo invocation would emit a fleet of
       packages that each build and express none of the dependencies the migration exists for.
       Asserted as `len(calls) == 1` *and* as both roots in that one argv — either alone passes
       over the arrangement the other forbids.
    2. *`-external=static`.* The default mode shells out to `go get`/`go list`/`git ls-remote`,
       resolves in a throwaway temp module that ignores the union `go.mod`'s pins entirely, and
       dropped a real dependency at exit 0. Static mode used zero subprocesses and zero network.
    3. *`-index=all`.* The default, restated so it cannot be removed: `-index=none` fabricates
       labels for directories that do not exist *and* drops real cross-repo edges.
    4. *`-repo_root=` and absolute directories under it.* Without it, run from a foreign working
       directory, the generator refuses with `not a subdirectory of repo root`.
    5. *A scratch tree, never a build worktree.* The generator WRITES, and pointing it at the tree
       Phase 3 is about to build and publish would make its output a build-system mutation of that
       tree — the exact class of thing ADR-0054 forbids from becoming a commit.
    6. *The scratch holds what the generator needs*: both repos' Go sources, the directives-only
       `BUILD.bazel` carrying `# gazelle:prefix` (without which every intra-repo import resolves
       to an unresolvable external one), and the fleet's root `go.mod`/`go.sum` — the module graph
       `-external=static` resolves every out-of-tree import against.

    **What this does NOT prove.** No real Gazelle ran, no Go compiled, and nothing here says the
    generator would in fact resolve these imports the way `FAKE_GAZELLE_OUTPUT` says. It proves
    what the harness does on both sides of the seam, which is the only part this suite owns.
    """
    add_repos(fleet, list(GO_DESTS))
    transformed(fleet)
    assert build(fleet, "--no-sandbox").exit_code == ExitCode.SUCCESS
    _ = (monorepo, bazel)

    assert len(gazelle.calls) == 1, (
        "the BUILD-file generator did not run exactly once; one invocation must cover every Go "
        "root at once, because an import of a sibling resolves to a real in-repo label ONLY when "
        "both roots are on the same command line and is silently dropped at exit 0 otherwise: "
        f"{[call.argv for call in gazelle.calls]}"
    )
    call = gazelle.calls[0]
    assert call.cwd is not None
    scratch = call.cwd
    assert list(call.argv) == [
        "gazelle",
        f"-repo_root={scratch}",
        "-external=static",
        "-index=all",
        f"{scratch}/go/clitool",
        f"{scratch}/go/digest",
    ], call.argv

    worktrees = {str(build_worktree(fleet, repo_id)) for repo_id in GO_DESTS}
    assert not any(str(scratch).startswith(path) for path in worktrees), (
        f"the generator was run inside a build worktree ({scratch}); it WRITES, so its output "
        "would be a build-system mutation of the tree Phase 3 publishes — which is exactly what "
        "the publish contract forbids from becoming a commit"
    )

    tree = gazelle.trees[0]
    assert tree["go.mod"] == GO_ROOT_GO_MOD, (
        "the generator ran without the fleet's union `go.mod` beside it, so `-external=static` "
        f"had no module graph to resolve any out-of-tree import against: {sorted(tree)}"
    )
    assert tree["go.sum"] == FAKE_LOCKS["go"][1], sorted(tree)
    assert "# gazelle:prefix github.com/acme/clitool" in tree["go/clitool/BUILD.bazel"], tree
    assert "# gazelle:prefix github.com/acme/digest" in tree["go/digest/BUILD.bazel"], tree
    assert tree["go/clitool/cmd/clitool/main.go"] == POLYGLOT_REPOS["acme-clitool-go"][
        "cmd/clitool/main.go"
    ], tree
    assert tree["go/digest/digest.go"] == POLYGLOT_REPOS["acme-digest-go"]["digest.go"], tree


def test_the_generated_go_build_files_carry_the_targets_the_generator_produced(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    gazelle: FakeGazelle,
    filter_repo: FakeFilterRepo,
) -> None:
    """**The replacement for the vacuity tripwire, which was designed to fail the day the
    generator was wired up. It has done its job; this is the assertion its docstring named.**

    The tripwire said, mechanically, that a Go package in this monorepo contained **zero targets
    of any kind** and that every non-header line of its `BUILD.bazel` was a `# gazelle:` comment —
    because `uses_gazelle = True` makes `generate_targets()` return `[]` and nothing ran the
    generator. It existed so that a "the Go repos build" test could not pass vacuously over a
    tree with no Go targets in it. The coverage is not dropped here, it is inverted: the same
    packages are now asserted to carry real targets with real dependency labels.

    **What is asserted, and why each part.**

    * `go_library(` and `load(` in the published bytes — the two strings the tripwire forbade.
    * The dependency labels by name: `@com_github_spf13_cobra//:go_default_library` and
      `@org_golang_x_crypto//blake2b:go_default_library`. One comes from each repo, so a capture
      that kept one repo's output and lost the other's fails here rather than passing on half a
      fleet — which is the same one-contributor defect ADR-0050 step 2 recorded for the root
      `go.mod`.
    * The in-tree label `//go/clitool/internal/command`, spelled as a monorepo path and not as a
      repo-relative one: the generator resolves against `# gazelle:prefix`, and a package that
      still named `internal/command` would be a label that does not exist in this tree.
    * `visibility = ["//go/clitool:__subpackages__"]` on `internal/`, which is the Go visibility
      rule expressed in Bazel and is a fact about where the file LANDED, not about its text.
    * The `# gazelle:prefix` directive **survives** into the modified `go/digest/BUILD.bazel`.
      That is the answer to "which bytes win" made mechanical: `buildgen` re-renders the
      directives file unconditionally on every GENERATE and `materialize` overwrites it from the
      captured bytes immediately afterwards, so the captured text wins — and it wins without
      losing the directives, because the generator preserves them when it rewrites the file
      (measured against the real binary in section 8, not assumed here).
    * `go/clitool/BUILD.bazel` is STILL directives-only, and that is correct rather than a
      shortfall: there is no `.go` file at that level, the real generator leaves it byte-identical
      and nothing is captured for it. It is asserted so that "captured everything" cannot be
      confused with "rewrote everything".

    **What this does NOT prove.** No real Gazelle ran — `cli.GAZELLE_RUNNER` is a fake, and the
    labels above are the fake's, chosen to match what the vendored binary was measured to emit
    over these exact fixtures. **No Go source was compiled by anything**, no `bazel build` ever
    looked at these targets (`FakeBazel` answers from a table), and nothing here says the labels
    resolve. A real-binary proof is a separate exercise; this is the harness's plumbing.
    """
    _ = bazel
    add_repos(fleet, list(GO_DESTS))
    transformed(fleet)
    assert build(fleet, "--no-sandbox").exit_code == ExitCode.SUCCESS
    assert set(relocations(filter_repo)) >= set(GO_DESTS)
    assert len(gazelle.calls) == 1, gazelle.calls

    binary = _generated(monorepo, "go/clitool/cmd/clitool/BUILD.bazel")
    internal = _generated(monorepo, "go/clitool/internal/command/BUILD.bazel")
    digest = _generated(monorepo, "go/digest/BUILD.bazel")

    generated = (("cmd/clitool", binary), ("internal/command", internal), ("digest", digest))
    for label, body in generated:
        assert "go_library(" in body, (label, body)
        assert "load(" in body, (label, body)

    assert '"@com_github_spf13_cobra//:go_default_library"' in internal, internal
    assert '"@org_golang_x_crypto//blake2b:go_default_library"' in digest, digest
    assert '"//go/clitool/internal/command"' in binary, (
        "the binary package's dependency on its own `internal/` package is not a monorepo label; "
        "the generator resolves against `# gazelle:prefix`, so a repo-relative spelling here is a "
        f"label that does not exist in this tree:\n{binary}"
    )
    assert 'visibility = ["//go/clitool:__subpackages__"]' in internal, internal
    assert "go_binary(" in binary, binary

    assert "# gazelle:prefix github.com/acme/digest" in digest, (
        "the captured bytes replaced the directives file rather than extending it, so the prefix "
        f"every intra-repo import resolves against is gone from the published file:\n{digest}"
    )
    parent = _generated(monorepo, "go/clitool/BUILD.bazel")
    assert "# gazelle:prefix github.com/acme/clitool" in parent, parent
    assert "go_library(" not in parent, (
        "`go/clitool` has no `.go` file of its own, so the generator leaves its BUILD file "
        f"byte-identical and nothing is captured for it:\n{parent}"
    )


def test_a_generated_sub_package_at_depth_lands_on_the_branch_with_its_planned_bytes(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    gazelle: FakeGazelle,
) -> None:
    """A captured BUILD file three levels below its `dest` is in the PUBLISH COMMIT's own tree,
    byte for byte as the generator wrote it.

    **The defect this closes, stated as the measurement that found it.** The publish step staged
    a single literal pathspec — `<dest>/BUILD.bazel` — which is exactly right while a package's
    generated content is one file at one depth and wrong the moment a generator writes packages of
    its own. Over a fixture-shaped fleet the generator CREATES `go/clitool/cmd/clitool/BUILD.bazel`
    and `go/clitool/internal/command/BUILD.bazel` and MODIFIES `go/digest/BUILD.bazel`, and a
    literal pathspec stages none of the two created ones: the branch would carry a `dest`-level
    file naming sub-packages that are not in the tree, and `bazel build //go/clitool/...` would
    fail over files that were generated, materialized, and then never committed.

    Asserted on the publish commit's own tree and not only on the branch tip, for the reason
    `test_publish_commits_the_planned_support_file_bytes…` gives: a later repo's correct publish
    must not be able to paper over an earlier repo's wrong one.

    **What this does NOT prove.** That the bytes are *right* — they are the fake's. Only that
    whatever the generator produced, at whatever depth, reaches the branch as planned bytes.
    """
    _ = bazel
    add_repos(fleet, list(GO_DESTS))
    transformed(fleet)
    assert build(fleet, "--no-sandbox").exit_code == ExitCode.SUCCESS
    assert len(gazelle.calls) == 1, gazelle.calls

    sha = publish_commit(monorepo, "go/clitool")
    for relative, text in sorted(FAKE_GAZELLE_OUTPUT["go/clitool"].items()):
        path = f"go/clitool/{relative}"
        assert path.count("/") >= 3, path
        # `SupportFile` is a `FleetModel` (`str_strip_whitespace=True`), so the planned bytes are
        # the generator's text stripped — pinned rather than papered over with an `rstrip()`.
        assert blob(monorepo, sha, path) == text.strip(), path
        assert _generated(monorepo, path) == text.strip(), path


def test_publish_re_materializes_a_generated_build_file_the_build_scribbled_on(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    monkeypatch: pytest.MonkeyPatch,
    filter_repo: FakeFilterRepo,
    resolver: FakeResolver,
    gazelle: FakeGazelle,
) -> None:
    """ADR-0054 over GENERATED BUILD files: a build that edits a captured sub-package file in the
    worktree publishes the PLANNED bytes anyway.

    **Why this test has to exist even though ADR-0054 is already held by a test.** The existing
    one damages *root support files* — `pnpm-lock.yaml`, `requirements.lock` — which is where the
    defect was found. The capture design puts a new KIND of content through the same contract:
    files a build-file generator produced, inside a repo's own package, at depth. If capturing
    had required an exception to "the planned bytes win" — staging the tree for these paths, say,
    because they are "the generator's" — the contract would have been weakened rather than
    honoured, and this test is what says it was not. The generator's output is a `SupportFile`
    like any other, so `_publish` re-materializes it like any other.

    `mutated` is checked so that a fake which quietly stopped writing would fail here rather than
    pass an assertion about a defect it never staged.

    **What this does NOT prove.** Nothing about Gazelle or Go: the generator is a fake, the Bazel
    that damages the tree is a fake, and no build ran.
    """
    _ = (filter_repo, resolver)
    damaged = "go/clitool/cmd/clitool/BUILD.bazel"
    planned = FAKE_GAZELLE_OUTPUT["go/clitool"]["cmd/clitool/BUILD.bazel"].strip()
    fake = WritebackBazel(fleet / "artifacts" / "fake-bazel", paths=[damaged])
    monkeypatch.setattr(cli, "BAZEL_RUNNER", fake)

    add_repos(fleet, list(GO_DESTS))
    transformed(fleet)
    assert build(fleet, "--no-sandbox").exit_code == ExitCode.SUCCESS
    assert len(gazelle.calls) == 1, gazelle.calls

    assert ("acme-clitool-go", damaged) in fake.mutated, (
        fake.mutated,
        "the fake did not actually damage the generated file, so a pass proves nothing",
    )
    sha = publish_commit(monorepo, "go/clitool")
    assert blob(monorepo, sha, damaged) == planned, damaged
    assert _generated(monorepo, damaged) == planned, damaged
    assert WRITEBACK_LINE.strip() not in _generated(monorepo, damaged)


def _go_build_plans(root: Path) -> dict[str, Any]:
    """Two `_BuildPlan`s over the Go fixtures' real sources on disk, for the driver-level test
    below. The `unit` and its coordinates come from the shipped `manifests/gomod.py`, and the
    `GazelleConfig` from the shipped adapter, so nothing about the flags or the directives is
    restated in the test.
    """
    ecosystems.discover()
    adapter = ecosystems.for_ecosystem(Ecosystem.GO)
    units = _go_units_from_fixture_manifests()
    roots = tuple(
        SupportFile(path=name, carry_from=[], content=content)
        for name, content in (("go.mod", GO_ROOT_GO_MOD), ("go.sum", FAKE_LOCKS["go"][1]))
    )
    plans: dict[str, Any] = {}
    for repo_id, dest in GO_DESTS.items():
        worktree = root / repo_id
        for name, text in POLYGLOT_REPOS[repo_id].items():
            path = worktree / dest / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        plans[repo_id] = cli._BuildPlan(
            repo_id=repo_id,
            dest=dest,
            worktree=worktree,
            integration_ref="refs/fleet/test/integration/1",
            integration_sha="0" * 40,
            merge_sha="1" * 40,
            source_sha="2" * 40,
            already_ingested=False,
            unit=units[repo_id],
            targets=(),
            workspace_deps=(),
            toolchains=(),
            workspace_files=roots,
            package_files=(),
            gazelle_files=(),
            root_targets=(),
            requirements=(),
            gazelle=adapter.gazelle_config(units[repo_id]),
            baseline_test_count=0,
            baseline_ok=None,
            adapter_name=adapter.name,
            adapter_degraded=False,
        )
    return plans


def test_the_captured_generator_output_is_a_function_of_the_fleet_not_of_plan_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The argv and the captured set are IDENTICAL whichever order the driver holds its plans in.

    **Why order is the thing worth pinning.** The plans reach this pass as a dict whose order is
    the order repos settled in — a function of wave membership, scheduling and how fast each
    dispatch returned. §11.6 requires one plan set to render one set of bytes in every process,
    and two properties here are order-sensitive by construction unless they are sorted: the
    ROOTS on the command line (the generator's own output was measured byte-identical across
    repeated clean runs, but only for one argv) and the CAPTURED files, which are attributed to
    repos by walking a tree. Nothing else in this suite would notice a regression in either: both
    orderings name the same repos and the same files, so every "is the sub-package there"
    assertion stays green while the published bytes differ between two processes.

    Run over two separate scratch directories, so the second run cannot inherit the first's
    output and agree with it for the wrong reason — which is also what asserts that the pass
    recreates its scratch rather than accumulating in it.

    **What this does NOT prove.** Nothing about the generator itself; it is the fake. This is a
    claim about the driver's own determinism.
    """
    plans = _go_build_plans(tmp_path / "worktrees")
    forward = [plans["acme-clitool-go"], plans["acme-digest-go"]]
    reverse = [plans["acme-digest-go"], plans["acme-clitool-go"]]
    seen: list[dict[str, tuple[str, ...]]] = []
    argvs: list[tuple[str, ...]] = []
    for index, order in enumerate((forward, reverse, forward)):
        fake = FakeGazelle()
        monkeypatch.setattr(cli, "GAZELLE_RUNNER", fake)
        scratch = tmp_path / f"scratch-{index}"
        captured = asyncio.run(
            cli._run_gazelle(
                order, binary="gazelle", scratch=scratch
            )
        )
        argvs.append(
            tuple(arg.replace(str(scratch), "<scratch>") for arg in fake.calls[0].argv)
            if fake.calls
            else ()
        )
        seen.append(
            {
                repo_id: tuple(f"{f.path}\n{f.content}" for f in files)
                for repo_id, files in sorted(captured.items())
            }
        )
    assert argvs[0] == argvs[1] == argvs[2], argvs
    assert argvs[0][-2:] == ("<scratch>/go/clitool", "<scratch>/go/digest"), (
        "the roots on the command line are in the order the driver happened to hold its plans "
        f"in, so one fleet produces two argvs across two processes: {argvs[0]}"
    )
    assert seen[0] == seen[1] == seen[2], seen
    assert set(seen[0]) == set(GO_DESTS), seen[0]
    assert seen[0]["acme-clitool-go"], (
        "nothing was captured for the repo whose BUILD files are all BELOW its dest; a capture "
        "of `<dest>/BUILD.bazel` alone finds that file byte-identical and looks like it worked"
    )


def test_the_driver_overlays_a_declared_resolver_env_onto_the_inherited_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`Resolution.env` reaches the runner MERGED OVER `os.environ` — the declared vars win, and
    everything the resolver needs to run at all survives.

    **Why the forward is a test and not an implementation detail.** `Resolution.env` was inert
    until a call site passed it: the adapter could declare `GOTOOLCHAIN=go1.24.12`, every purity
    and contract assertion in `test_ecosystems.py` would stay green, and `go mod download all`
    would still have run under whatever the host exported. This is the assertion that the pin is
    actually applied, taken at the seam that sees exactly what a real `run()` would have got.

    **Why OVERLAY and not replacement, asserted in both directions.** `util/proc.run` hands its
    `env=` straight to `create_subprocess_exec`, which REPLACES the child's environment rather
    than extending it — so a driver that forwarded `plan.env` alone would launch a `go` with no
    `PATH` to be found on and no `HOME` to reach its module cache under. That failure does not
    look like a missing variable; it surfaces as `FileNotFoundError`, which `_run_resolution`
    reports as "`go` is not installed on this host", sending an operator to install a toolchain
    that is already there. So: the declared var must be present, an inherited var the resolver
    needs must survive, and — the third leg, and the whole reason the field exists — the declared
    value must BEAT a conflicting inherited one, because `GOTOOLCHAIN=auto` in the operator's
    shell is precisely the ambient fact these sums must stop depending on.
    """
    ecosystems.discover()
    adapter = ecosystems.for_ecosystem(Ecosystem.GO)
    unit = BuildUnit(
        unit_id="acme-commons-go",
        ecosystem=Ecosystem.GO,
        dest="go/commons",
        srcs=["commons.go"],
        published=Coordinate(ecosystem=Ecosystem.GO, group="github.com/acme", name="commons"),
        external_coordinates=[
            Coordinate(
                ecosystem=Ecosystem.GO,
                group="github.com/stretchr",
                name="testify",
                version_spec="v1.9.0",
            )
        ],
    )
    worktree = tmp_path / "worktree"
    package = worktree / unit.dest
    package.mkdir(parents=True)
    (package / "go.mod").write_text(
        "module github.com/acme/commons\n\ngo 1.23.4\n\n"
        "require github.com/stretchr/testify v1.9.0\n",
        encoding="utf-8",
    )

    # The two ambient facts this test is about: a variable the resolver cannot run without, and
    # the exact variable the adapter is pinning, exported by the "operator" to the value that
    # makes the toolchain choice a property of the host.
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin"))
    monkeypatch.setenv("GOTOOLCHAIN", "auto")
    monkeypatch.setenv("FLEET_TEST_INHERITED", "still-here")

    resolver = FakeResolver()
    monkeypatch.setattr(cli, "RESOLVER_RUNNER", resolver)
    asyncio.run(
        cli._resolved_support_files(
            adapter,
            [unit],
            repo_id=str(unit.unit_id),
            worktrees=[worktree],
            scratch=tmp_path / "scratch",
        )
    )

    (call,) = resolver.calls
    assert call.env is not None, "the driver handed the runner no environment at all"
    (sdk,) = adapter.toolchain_requirements()
    assert call.env["GOTOOLCHAIN"] == f"go{sdk.version}", (
        "the declared pin must beat the inherited `auto`; otherwise the sums are hashes taken "
        "under whichever toolchain the host happened to download"
    )
    assert call.env["PATH"] == os.environ["PATH"], (
        "a replaced environment strips `PATH`, and `go` then reports as 'not installed'"
    )
    assert call.env["FLEET_TEST_INHERITED"] == "still-here", (
        "the merge must be an overlay: everything not declared is inherited unchanged"
    )
    # …and the process this harness runs in is untouched. The overlay is built for the child;
    # mutating `os.environ` would make one ecosystem's resolve reconfigure the next one's.
    assert os.environ["GOTOOLCHAIN"] == "auto"


def test_a_resolver_without_a_declared_env_still_inherits_the_operators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `uv` path, which declares nothing, is unchanged by the overlay existing.

    **Why:** `Resolution.env` defaults to empty for every adapter but Go, and the merge must be a
    no-op in that case rather than "no environment". Passing `env={}` through to `run()` would
    replace the environment with an empty one for *every* resolver that never opted in — Python
    and JS both — turning a change that was supposed to touch only Go into a fleet-wide
    `uv: command not found`. That is the regression this case exists to catch, and it is
    invisible to any assertion about Go.
    """
    ecosystems.discover()
    adapter = ecosystems.for_ecosystem(Ecosystem.PYPI)
    unit = BuildUnit(
        unit_id="acme-app-py",
        ecosystem=Ecosystem.PYPI,
        dest="py/acme_app_py",
        external_coordinates=[
            Coordinate(ecosystem=Ecosystem.PYPI, name="requests", version_spec=">=2.31")
        ],
    )
    monkeypatch.setenv("FLEET_TEST_INHERITED", "still-here")

    resolver = FakeResolver()
    monkeypatch.setattr(cli, "RESOLVER_RUNNER", resolver)
    asyncio.run(
        cli._resolved_support_files(
            adapter,
            [unit],
            repo_id=str(unit.unit_id),
            worktrees=[tmp_path / "empty"],
            scratch=tmp_path / "scratch",
        )
    )

    (call,) = resolver.calls
    assert call.argv[0] == "uv"
    assert call.env is not None and call.env["FLEET_TEST_INHERITED"] == "still-here"
    assert "PATH" in call.env, "an adapter that declares no env must still get the operator's"
    assert "GOTOOLCHAIN" not in {k for k in call.env if k not in os.environ}, (
        "one ecosystem's declared pin must not leak into another's resolve"
    )


def test_a_resolver_failure_is_loud_and_classified_and_never_an_empty_lock(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolver that exits non-zero abandons THAT repo with a `DependencyResolutionFailed`
    finding naming the command and its stderr — and writes no lockfile at all.

    **Why this is the assertion and not "an error was logged":** the failure mode this whole path
    exists to close is a resolver failure that falls back to the synthesized floor. That fallback
    produces a syntactically valid lock, a dependency hub with nothing in it, and a build that
    fails minutes later inside a ruleset with a message about a package nobody declared — an
    empty lock is indistinguishable from "this repo has no dependencies". So the failure has to be
    *here*, at the repo that needed the resolution, and the repo must not reach Phase 3 green.

    Containment is asserted too (§11.1): the sibling repos of the same fleet still succeed, so one
    unreachable package index costs exactly the repos that needed it.
    """

    class BrokenResolver:
        async def __call__(
            self,
            argv: Sequence[str],
            *,
            cwd: Path | None = None,
            env: Mapping[str, str] | None = None,
            deadline: float | None = None,
            timeout_s: float | None = None,
        ) -> ProcResult:
            _ = (env, deadline, timeout_s)
            if argv[0] != "uv":
                return await FakeResolver()(argv, cwd=cwd)
            return ProcResult(
                argv=tuple(argv),
                exit_code=2,
                stdout_tail="",
                stderr_tail="error: no solution found: requests>=2.31 and urllib3<2 conflict",
                duration_ms=5,
                timed_out=False,
                cwd=cwd,
            )

    monkeypatch.setattr(cli, "BAZEL_RUNNER", FakeBazel(fleet / "artifacts" / "fake-bazel"))
    monkeypatch.setattr(cli, "FILTER_REPO_RUNNER", FakeFilterRepo())
    monkeypatch.setattr(cli, "RESOLVER_RUNNER", BrokenResolver())

    transformed(fleet)
    result = build(fleet, "--no-sandbox")
    assert result.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION, result.output

    findings = {
        str(row[0]): str(row[1])
        for row in query(fleet, "SELECT repo_id, kind FROM findings WHERE repo_id IS NOT NULL")
    }
    assert findings.get("acme-app-py") == "DependencyResolutionFailed", findings
    detail = json.dumps(
        [
            row[0]
            for row in query(
                fleet, "SELECT payload FROM findings WHERE repo_id = 'acme-app-py'"
            )
        ]
    )
    assert "uv pip compile" in detail, detail
    assert "no solution found" in detail, detail

    statuses = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 3"))
    assert statuses["acme-app-py"] != "SUCCEEDED", statuses
    assert statuses["acme-lib-ts"] == "SUCCEEDED", (
        "one unreachable package index must cost exactly the repos that needed it"
    )
    # …and nothing wrote a lock. A `requirements.lock` on the branch here would be the floor
    # wearing a resolution's name, which is the exact defect.
    listed = git(monorepo, "ls-tree", "--name-only", "integration").split()
    assert "requirements.lock" not in listed, listed


def _root_file_plan(unit: BuildUnit, worktree: Path, adapter_name: str) -> cli._BuildPlan:
    """A prepared plan carrying only what `_fleet_support_files` reads: the unit, the worktree the
    carry step scans, and the adapter the group is keyed on.

    `workspace_files=()` is not a shortcut — it is what `_plan_build` really leaves behind
    (ADR-0048): the root files are fleet-wide, so they are filled in once per ecosystem after the
    wave's plans exist, which is the function under test.
    """
    return cli._BuildPlan(
        repo_id=str(unit.unit_id),
        dest=unit.dest,
        worktree=worktree,
        integration_ref="refs/fleet/test/integration/0",
        integration_sha="a" * 40,
        merge_sha="b" * 40,
        source_sha="c" * 40,
        already_ingested=False,
        unit=unit,
        targets=(),
        workspace_deps=(),
        toolchains=(),
        workspace_files=(),
        package_files=(),
        root_targets=(),
        requirements=(),
        gazelle=None,
        baseline_test_count=0,
        baseline_ok=None,
        adapter_name=adapter_name,
        adapter_degraded=False,
    )


def test_an_unrenderable_coordinate_is_contained_to_its_own_ecosystems_repos(
    fleet: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Go coordinate `go.py` refuses to render costs the GO repos their root files — and the
    Python repo in the same call still gets its resolved `requirements.lock`.

    **What was broken.** `go.py` raises when a coordinate claiming `Ecosystem.GO` cannot be a
    `require` line, which is right: writing it out produces a root `go.mod` that `go` rejects at
    PARSE time, naming a file this harness invented. But `_fleet_support_files` caught only
    `DependencyResolutionError`, so the adapter's exception propagated out of the driver and out
    of `build` itself — ONE malformed coordinate ended the whole fleet's run. Rule 11 asks for the
    other shape: mark the affected repos and move to the next item. Loud and global are different
    things, and the assertion that separates them is the `files` half below, not the raise.

    **Attributed to every repo of the ecosystem, exactly as a resolver failure is.** The root
    `go.mod` is ONE file for the fleet, so a render that fails fails for all of its repos at once;
    reporting it against whichever repo contributed the coordinate would leave the others building
    against a root file that was never written.

    **Driven at `_fleet_support_files` rather than through a Go repo in the fixture fleet**, for
    the boundary `test_the_go_root_sum_is_resolved_from_the_union_go_mod_and_never_carried_itself`
    already names: this drives ONE driver function over hand-built plans, so the failure is
    observed where it is contained rather than through a whole run whose BUILD-file generation,
    ingest and dispatch would all have to succeed first. The full-chain containment — findings,
    phase rows and
    the sibling repos' verdicts — is
    `test_a_coordinate_render_failure_marks_its_repos_and_the_rest_of_the_fleet_builds` below.
    """
    ecosystems.discover()
    monkeypatch.setattr(cli, "RESOLVER_RUNNER", FakeResolver())
    settings = FleetSettings.load(fleet / "config")

    # `left-pad` has no dot in its first path element, so `go` refuses the whole file at parse
    # time — the coordinate that used to be written into the fleet's root module file.
    unrenderable = Coordinate(
        ecosystem=Ecosystem.GO, group="", name="left-pad", version_spec="v1.3.0"
    )
    plans = {
        "acme-commons-go": _root_file_plan(
            BuildUnit(
                unit_id="acme-commons-go",
                ecosystem=Ecosystem.GO,
                dest="go/commons",
                srcs=["commons.go"],
                external_coordinates=[unrenderable],
            ),
            tmp_path / "go-commons",
            "go",
        ),
        "acme-svc-go": _root_file_plan(
            BuildUnit(
                unit_id="acme-svc-go",
                ecosystem=Ecosystem.GO,
                dest="go/svc",
                srcs=["svc.go"],
                external_coordinates=[
                    Coordinate(
                        ecosystem=Ecosystem.GO,
                        group="github.com/stretchr",
                        name="testify",
                        version_spec="v1.9.0",
                    )
                ],
            ),
            tmp_path / "go-svc",
            "go",
        ),
        "acme-app-py": _root_file_plan(
            BuildUnit(
                unit_id="acme-app-py",
                ecosystem=Ecosystem.PYPI,
                dest="py/acme_app_py",
                srcs=["acme_app_py/__init__.py"],
                external_coordinates=[
                    Coordinate(ecosystem=Ecosystem.PYPI, name="requests", version_spec=">=2.31")
                ],
            ),
            tmp_path / "py-app",
            "py",
        ),
    }
    for plan in plans.values():
        plan.worktree.mkdir(parents=True)

    files, failures = asyncio.run(
        cli._fleet_support_files(plans, settings=settings, run_id="run-render")
    )

    # The whole point: the fleet's other ecosystem is untouched and really resolved.
    assert set(files) == {"acme-app-py"}, files
    resolved = {support.path: support.content for support in files["acme-app-py"]}
    assert resolved["requirements.lock"] == FAKE_LOCKS["uv"][1], resolved

    # …and BOTH Go repos are the ones that paid, including the one whose own coordinate is fine:
    # they share the root `go.mod`, so neither of them has one.
    assert set(failures) == {"acme-commons-go", "acme-svc-go"}, failures
    failure = failures["acme-commons-go"]
    assert failures["acme-svc-go"] is failure, "one root file, one verdict for its ecosystem"
    assert isinstance(failure, cli.CoordinateRenderError), type(failure)
    assert not isinstance(failure, cli.DependencyResolutionError), (
        "a coordinate this fleet's own inventory cannot spell is not a package index failing to "
        "answer; the two send an operator to two different places"
    )
    assert isinstance(failure.__cause__, go_adapter.GoModuleCoordinateError), failure.__cause__

    # Loud and attributable: the coordinate the adapter named, and the repos it was rendering for.
    detail = str(failure)
    assert "left-pad" in detail, detail
    assert "acme-commons-go" in detail and "acme-svc-go" in detail, detail
    assert "requests" not in detail, "the Python group's resolve is not part of this failure"


def test_a_coordinate_render_failure_marks_its_repos_and_the_rest_of_the_fleet_builds(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same containment through the WHOLE chain: `REQUIRES_HUMAN_INTERVENTION` and a
    `CoordinateRenderFailed` finding for that ecosystem's repos, `SUCCEEDED` for every other.

    **This is the assertion the round exists for.** The type of the exception proves nothing about
    blast radius; a run that ends on the first unrenderable coordinate raises exactly the same
    class. What separates "fail loud" from "fail global" is that the sibling repos of another
    ecosystem still reach Phase 3 green in the same `fleet build`, and that is asserted here.

    **The adapter is patched rather than a Go repo added to the fixture fleet**, and the exception
    raised is the NEUTRAL `ecosystems.AdapterCoordinateError` — not `go.py`'s subclass. That is
    the invariant, not a convenience: the driver contains this failure without knowing which
    ecosystem produced it (§12.6), so any adapter that refuses to render a coordinate is contained
    the same way, and a driver that had grown an `if ecosystem == GO` would still pass a test
    written with a Go repo while failing this one.
    """
    ecosystems.discover()
    adapter = ecosystems.for_ecosystem(Ecosystem.PYPI)
    coordinate = Coordinate(ecosystem=Ecosystem.PYPI, name="left-pad", version_spec="^1.3.0")

    def refuse(self: object, units: Sequence[BuildUnit]) -> list[Any]:
        _ = (self, units)
        raise ecosystems.AdapterCoordinateError(
            f"{coordinate.key}: {coordinate.version_spec!r} is an npm range and not a PEP 440 "
            f"specifier, so it cannot be a line in the monorepo's root requirements"
        )

    monkeypatch.setattr(type(adapter), "workspace_files", refuse)
    monkeypatch.setattr(cli, "BAZEL_RUNNER", FakeBazel(fleet / "artifacts" / "fake-bazel"))
    monkeypatch.setattr(cli, "FILTER_REPO_RUNNER", FakeFilterRepo())
    monkeypatch.setattr(cli, "RESOLVER_RUNNER", FakeResolver())

    transformed(fleet)
    result = build(fleet, "--no-sandbox")
    assert result.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION, result.output

    findings = {
        str(row[0]): str(row[1])
        for row in query(fleet, "SELECT repo_id, kind FROM findings WHERE repo_id IS NOT NULL")
    }
    assert findings.get("acme-app-py") == "CoordinateRenderFailed", findings
    assert findings.get("acme-lib-py") == "CoordinateRenderFailed", findings
    assert "DependencyResolutionFailed" not in set(findings.values()), (
        "no resolver ever ran, so telling an operator to go and look at a package index is a "
        "wrong answer, not a coarse one"
    )
    detail = json.dumps(
        [
            row[0]
            for row in query(fleet, "SELECT payload FROM findings WHERE repo_id = 'acme-app-py'")
        ]
    )
    assert coordinate.key in detail, detail
    assert "acme-app-py" in detail, detail

    statuses = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 3"))
    assert statuses["acme-app-py"] == "REQUIRES_HUMAN_INTERVENTION", statuses
    assert statuses["acme-lib-py"] == "REQUIRES_HUMAN_INTERVENTION", statuses
    assert statuses["acme-lib-ts"] == "SUCCEEDED", (
        "one unrenderable coordinate must cost exactly the repos of the ecosystem whose root "
        "file could not be written — the run continues for everyone else (Rule 11)"
    )
    assert statuses["acme-app-ts"] == "SUCCEEDED", statuses
    # …and the surviving ecosystem's own root file really landed, so 'the rest of the fleet
    # proceeded' is a build that happened rather than a phase row that was never written to.
    listed = git(monorepo, "ls-tree", "--name-only", "integration").split()
    assert "pnpm-lock.yaml" in listed, listed
    assert "requirements.lock" not in listed, listed


# ---------------------------------------------------------------------------------------
# 7. the real binaries — what they prove, and the defects they found
# ---------------------------------------------------------------------------------------
# `bazel` and `git-filter-repo` are BOTH installed here now (see `tests/conftest.py`), so
# neither of these skips. Running them for real found live `src/` defects that no fake could
# have shown, which is the whole argument for keeping these tests. Every defect
# `test_build_against_a_real_bazel` used to pin is now guarded by a test that fails rather than
# by a marker that passes: it puts all four fixture repos through a real Bazel 9.2.0 and asserts
# on its exit status. NO `xfail` is left in this section. The last one held
# `test_two_js_repos_with_different_npm_dependencies_both_build` — a defect that was never
# reproduced, only read, until a fifth repo made it reachable — and it was `strict=True` so it
# could not outlive the bug; ADR-0048 fixed the bug and the marker went with it.
# `docs/INTEGRATION_HONESTY.md` carries the full ledger.


@pytest.mark.integration
@pytest.mark.skipif(
    shutil.which("bazel") is None,
    reason="bazel is not installed on this host, so there is no version to compare against",
)
def test_the_fixture_runs_the_bazel_this_suite_verifies_against(monorepo: Path) -> None:
    """`make_monorepo`'s `.bazelversion` must be the version `tools/bin/bazel` actually launches.

    **Why:** bazelisk obeys `.bazelversion`, so this one file decides which Bazel the entire
    end-to-end pipeline runs — and while it said `7.4.1` the pipeline ran a toolchain that
    nothing else in the suite, and nothing in production, uses. That is not a stale constant; it
    is a *mask*. `test_bazel.py` found `aspect_rules_ts@3.5.0` unloadable under 9.2.0 (Bazel 9
    removed `@local_config_platform`) and the e2e test could not have: 7.4.1 still had that
    repository. A pin that makes one test blind to a defect another test can see is worth an
    explicit assertion, not a comment.

    It compares against the binary rather than restating 9.2.0 in a second place, so upgrading
    the toolchain fails HERE — one file, one diff — instead of somewhere twenty minutes into a
    build with a message about a ruleset.
    """
    reported = subprocess.run(
        ["bazel", "--version"],  # noqa: S607  (`tools/bin` is on PATH via conftest)
        cwd=monorepo,
        capture_output=True,
        text=True,
        check=True,
        timeout=300.0,
    ).stdout
    assert reported.split() == ["bazel", MONOREPO_BAZEL_VERSION], reported
    assert (monorepo / ".bazelversion").read_text(encoding="utf-8").strip() == (
        MONOREPO_BAZEL_VERSION
    )


def real_build(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel_cache_home: Path,
    bazel_registry: str,
    bazel_fetch_bazelrc: str,
    *extra: str,
) -> Any:
    """`scan → sequence → transform → build` with **no seam installed**, and the result of `build`.

    Shared by the two real-Bazel tests below so that one function decides what "the real pipeline"
    means. The seam assertions are inside it for the same reason: a caller that forgot to check
    would be asserting on `FakeBazel`'s answer table and could not tell.

    The `.bazelrc` has to be COMMITTED — Phase 3 cuts its worktree from the `integration` tip, so
    an untracked file in the monorepo root is not in the tree the build ever sees — and it carries
    two unrelated things: the registry (`bazel_registry`, because the default BCR address is not
    always reachable here) and the fetch tuning (`bazel_fetch_bazelrc`, because a GitHub release
    redirect on this host outlasts Bazel's default HTTP timeout). Neither changes what Bazel
    analyses; both stop a network symptom from impersonating a defect in `src/`.

    `bazel_cache_home` is taken as an argument rather than used: `fleet build` owns every `bazel`
    argv here, so the only way to move Bazel's output base is the `XDG_CACHE_HOME` that fixture
    sets, and naming it here is what makes the caller pass it in.

    **`verify.repository_cache` is pointed at the session's archive cache, and that is not
    tidiness.** The harness now emits `--repository_cache=` from that setting (it used to mount
    the directory and never name it), and a flag on the command line BEATS the `common` line in
    the `.bazelrc` written just below — so leaving the default would give every one of these
    tests its own empty archive cache under the per-test workspace and make each of them re-fetch
    the ~3 minutes of ruleset tarballs `bazel_fetch_bazelrc` exists to pay once per checkout,
    from a live registry, which is also the flake this file's registry fixture was written about.
    Written BEFORE `transformed()` so the run records its digests over the config it will use.
    """
    config = fleet / "config" / "fleet.yaml"
    config.write_text(
        f"{config.read_text(encoding='utf-8')}verify:\n"
        f"  repository_cache: {BAZEL_REPOSITORY_CACHE}\n",
        encoding="utf-8",
    )
    (monorepo / ".bazelrc").write_text(
        f"common --registry={bazel_registry}\n{bazel_fetch_bazelrc}", encoding="utf-8"
    )
    git(monorepo, "add", "-A")
    git(monorepo, "commit", "-m", "pin the module registry for this run")
    transformed(fleet)
    assert cli.BAZEL_RUNNER is None, "the seam must be absent for this test to mean anything"
    assert cli.FILTER_REPO_RUNNER is None, "the seam must be absent for this test to mean anything"
    # The third seam, and the one this function is now also paying for: with `RESOLVER_RUNNER`
    # set, the lockfiles Bazel reads would be `FakeResolver`'s canned text and the closure below
    # would prove nothing about a resolver ever having run.
    assert cli.RESOLVER_RUNNER is None, "the seam must be absent for this test to mean anything"
    # `*extra` is how a caller narrows the fleet (`--repo`), and nothing else: every seam
    # assertion above still runs, so a narrowed build is as real as a whole-fleet one.
    return build(fleet, "--no-sandbox", *extra)


@pytest.mark.integration
@pytest.mark.skipif(
    shutil.which("bazel") is None,
    reason=(
        "bazel is not installed on this host and this harness does not install it; every other "
        "test in this file drives the bazel invocations through cli.BAZEL_RUNNER, which proves "
        "the argv, the exit-code handling, the parsing and the state transitions but NOT that a "
        "build system ever looked at the tree"
    ),
)
def test_build_against_a_real_bazel(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    tmp_path: Path,
    bazel_cache_home: Path,
    bazel_registry: str,
    bazel_fetch_bazelrc: str,
    bazel_startup_argv: tuple[str, ...],
) -> None:
    """The test that proves a build. `fleet build` with **no seam installed**, so real `bazel`
    and real `git-filter-repo` run against the real merged tree.

    Bazel's output base is redirected out of `~/.cache` via `XDG_CACHE_HOME` — the suite must not
    write outside its own workspace, and a 5 GB action cache in a developer's home directory is
    not something a test gets to create silently. It now lands in the `bazel_cache_home` fixture's
    shared Bazel root, which is reaped when the test ends, rather than in a per-test directory
    pytest keeps for three sessions — the same 5 GB, retained, was how a full run filled the disk.

    The module registry goes into the monorepo's `.bazelrc` rather than onto a command line
    because `fleet build` owns every `bazel` invocation here — there is no argv for a test to
    add a flag to. It has to be COMMITTED: Phase 3 cuts its worktree from the `integration` tip,
    so an untracked `.bazelrc` in the monorepo root is not in the tree the build ever sees.

    **It was `xfail(strict=True)` through eleven defects and it is not any more: all four repos
    build green.** The marker is deleted rather than left passing, so this is now a guard: the
    assertion is Bazel's own exit status over a tree Bazel actually analysed, and every defect
    the xfail reason used to enumerate — D3's double relocation, D5/D6/D7/D9/D10/D11, the missing
    resolver, and finally D12's first-party linking — turns it red again the moment it returns.
    It is the ONLY thing in this suite that could ever have caught any of them: `FakeFilterRepo`
    does not move a single path and `FakeBazel` answers from a table without reading the tree.

    The last of those, D12, is the one to keep in view when reading this assertion: a green exit
    here means `tsc` **type-checked** `import { formatMoney } from '@acme/lib'` in
    `ts/acme/app/src/main.ts` against declarations it resolved through
    `//ts/acme/app:node_modules/@acme/lib`. `ts_project` fails the action on a type error, so a
    cross-repo first-party import that did not resolve cannot reach exit 0 — which is why this,
    and not any query over the generated files, is the test that says the monorepo links.
    """
    result = real_build(fleet, monorepo, bazel_cache_home, bazel_registry, bazel_fetch_bazelrc)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    # ... and then ask Bazel directly, over the tree the four repos actually merged into.
    #
    # The exit code above is the harness's summary of what it was told; this is the build system's
    # own answer about its own tree, and the two are not the same assertion. Every defect in the
    # D1–D11 series existed because something was satisfiable without a build happening, and one
    # guard in this suite was satisfied vacuously by the very bug it guarded — so the last word
    # here belongs to `bazel`, run over `integration` with no harness in the path.
    #
    # `//...` and not four package patterns: it is the whole monorepo, so a repo that silently
    # generated nothing is a repo with no targets rather than a green pattern, and the four
    # destinations are checked to be present in the tree first for exactly that reason.
    checkout = tmp_path / "integration-checkout"
    git(monorepo, "worktree", "add", "--detach", str(checkout), "integration")
    for dest in DESTINATIONS.values():
        assert (checkout / dest / "BUILD.bazel").is_file(), f"{dest} generated no BUILD.bazel"
    built = subprocess.run(  # noqa: S603
        [*bazel_startup_argv, "build", "//..."],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
        timeout=1800.0,
    )
    assert built.returncode == 0, built.stderr[-8000:]
    assert "Build completed successfully" in built.stderr, built.stderr[-8000:]
    # The TypeScript consumer's action RAN and its compiler accepted the cross-repo import: a
    # `ts_project` fails the action on a type error, so `//ts/acme/app:app` producing its output
    # is the statement that `tsc` resolved `'@acme/lib'` through
    # `//ts/acme/app:node_modules/@acme/lib`.
    # This is the assertion the old `xfail` reason described as impossible.
    assert (checkout / "bazel-bin/ts/acme/app/src/main.js").is_file(), built.stderr[-8000:]


@pytest.mark.integration
@pytest.mark.skipif(
    shutil.which("bazel") is None,
    reason=(
        "bazel is not installed on this host; the whole point of this test is that a real bazel "
        "reads the unknown-ecosystem repo's generated filegroup and exits 0 over it, which "
        "`test_an_unknown_ecosystem_still_falls_back_visibly` cannot show through `FakeBazel`'s "
        "canned answer table"
    ),
)
def test_the_unknown_ecosystem_filegroup_builds_under_a_real_bazel(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    tmp_path: Path,
    bazel_cache_home: Path,
    bazel_registry: str,
    bazel_fetch_bazelrc: str,
    bazel_startup_argv: tuple[str, ...],
) -> None:
    """SPEC §12.25's other half: `test_an_unknown_ecosystem_still_falls_back_visibly` proves the
    no-manifest/`filegroup`/dest/finding shape end-to-end, but through `FakeBazel` — a canned
    answer table, not a build system that ever read the tree. This is the same claim asked of a
    real `bazel build //...`: `acme-runbooks` (no manifest any adapter recognizes) still gets a
    `no-manifest` finding, `Ecosystem.UNKNOWN`, `dest='misc/acme-runbooks'`, `wave_index=0`, a
    `filegroup` BUILD target that a real Bazel accepts with exit 0, and a row in
    `migration_state.json` — the same checklist SPEC §12 item 25 states, over the artifact real
    Bazel produced rather than a fake's table.

    **`add_repos`, not `only_repos`.** `acme-runbooks` is grown onto the base
    `tests.test_scan_e2e.FIXTURE_REPOS` fleet exactly as `test_two_js_repos_with_different_npm_
    dependencies_both_build` and `test_two_python_repos_with_different_pypi_dependencies_both_
    build` grow it for their own added repos — a new, isolated test function rather than an
    extra assertion block inside `test_build_against_a_real_bazel`, because that test's own
    `set(merged) == set(DESTINATIONS)` parity assertion is keyed on the exact 4-member
    `FIXTURE_REPOS` set and growing it in place would need `DESTINATIONS` to grow too, which
    risks perturbing an assertion this task was told not to touch. `acme-runbooks` declares no
    external dependency and its adapter fetches no toolchain (`workspace_deps` returns `[]`,
    `ruleset`/`extension`/`repo_name` are all `None`), so unlike the Rust fixture two tests below,
    growing the fleet by one repo here does not risk this session's shared Bazel output-base
    ceiling.
    """
    add_repos(fleet, ["acme-runbooks"])
    result = real_build(fleet, monorepo, bazel_cache_home, bazel_registry, bazel_fetch_bazelrc)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    # -- Phase 1: the no-manifest finding, and the ecosystem it was classified into ------------
    ecosystems.discover()
    assert repo_ecosystem(fleet, "acme-runbooks") is Ecosystem.UNKNOWN
    unknown = ecosystems.for_ecosystem(Ecosystem.UNKNOWN)
    dest = f"{unknown.monorepo_dir}/acme-runbooks"
    assert dest == "misc/acme-runbooks", dest

    no_manifest_rows = query(fleet, "SELECT repo_id FROM findings WHERE kind = 'no-manifest'")
    assert [str(row[0]) for row in no_manifest_rows] == ["acme-runbooks"], no_manifest_rows
    degraded_rows = query(
        fleet, "SELECT repo_id, payload FROM findings WHERE kind = 'EcosystemAdapterUnavailable'"
    )
    assert [str(row[0]) for row in degraded_rows] == ["acme-runbooks"], degraded_rows
    assert json.loads(str(degraded_rows[0][1]))["dest"] == dest, degraded_rows

    # -- §3.1 step 7: no dependencies ⇒ wave 0 --------------------------------------------------
    assert wave_index(fleet, "acme-runbooks") == 0, "an unknown repo with no deps must land wave 0"

    # -- Phase 3, over the real artifact: the generated BUILD.bazel and a real bazel build ------
    checkout = tmp_path / "integration-checkout"
    git(monorepo, "worktree", "add", "--detach", str(checkout), "integration")
    for known_dest in DESTINATIONS.values():
        assert (checkout / known_dest / "BUILD.bazel").is_file(), (
            f"{known_dest} generated no BUILD.bazel"
        )
    body = (checkout / dest / "BUILD.bazel").read_text(encoding="utf-8")
    assert "filegroup(" in body, body
    assert "fleet_adapter=unknown" in body, body

    built = subprocess.run(  # noqa: S603
        [*bazel_startup_argv, "build", "//..."],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
        timeout=1800.0,
    )
    assert built.returncode == 0, built.stderr[-8000:]
    assert "Build completed successfully" in built.stderr, built.stderr[-8000:]

    # Scoped to just this repo's own target, so a green `//...` above cannot be hiding a
    # `filegroup` that Bazel silently skipped because nothing else in the graph reaches it: this
    # is the assertion the SPEC's "a `filegroup` BUILD target that builds green" clause makes,
    # asked directly of the one target rather than inferred from the whole monorepo's exit code.
    scoped = subprocess.run(  # noqa: S603
        [*bazel_startup_argv, "build", f"//{dest}:acme-runbooks"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
        timeout=300.0,
    )
    assert scoped.returncode == 0, scoped.stderr[-8000:]
    assert "Build completed successfully" in scoped.stderr, scoped.stderr[-8000:]

    # -- migration_state.json: the repo is neither dropped nor a crash -------------------------
    projection = fleet / "migration_state.json"
    state = MigrationState.model_validate_json(projection.read_text(encoding="utf-8"))
    assert "acme-runbooks" in state.repos, sorted(state.repos)
    assert state.repos["acme-runbooks"].status.value == "SUCCEEDED", state.repos["acme-runbooks"]


#: The two Rust fixtures' destinations. `rust/<crate>` is the RustAdapter's own answer
#: (`monorepo_dir` + `path_tail`), restated here only so the assertions below can be read.
RUST_DESTS: tuple[str, ...] = ("rust/acme-case-rs", "rust/acme-codec-rs")

RUST_MODULE_LOCK_FIXTURE: Path = Path(__file__).parent / "fixtures" / "rust" / "MODULE.bazel.lock"
"""`MODULE.bazel.lock` for the exact `only_repos(fleet, ["acme-codec-rs", "acme-case-rs"])`
monorepo below, captured VERBATIM from a real, networked `bazel build` of this same fixture pair
(research-38 §Q1, run against `BCR_DEFAULT_REGISTRY`).

**Committed as fixture data, not generated at test setup.** Generating it in a setup step would
mean running the very networked `cargo fetch` this file exists to avoid on every test run — a
"fix" that only moves the flake from inside the assertion to inside the fixture and proves
nothing. A committed lock is the only one of the two shapes that actually removes the network
dependency: research-38 measured a build with this exact artifact present succeeding OFFLINE in
32.4 s (run2) and reproduced today's flake byte-for-byte with it invalidated (run4,
`Failed to fetch crates for lockfile: exit status: 101`).

**Why seeding it works even though the Rust e2e build is always a "first" build.** `rust.py`
emits no `crate.from_cargo(lockfile=...)` attribute, so `repin` is unconditionally `True` and the
extension always re-evaluates (`crate_universe/extensions.bzl`'s own
`repin = not lockfile or determine_repin(...)`) — but re-evaluating is not the same as
re-SPLICING. Bazel replays a module extension's `generatedRepoSpecs` from `MODULE.bazel.lock`
whenever the extension's `recordedInputs` (env vars, repo mapping, and hashes of `//:Cargo.toml`,
`//:Cargo.lock` and both member manifests) still match what it reads, and only falls through to
`cargo-bazel splice` — the step that opens a socket to `index.crates.io` — when they do not. This
fixture's `recordedInputs` were captured against these exact fixture files, so seeding it before
the FIRST `fleet build` on a fresh monorepo turns that first evaluation into a replay too.

**It goes stale, and that is `_assert_no_cargo_splice` below's whole job.** The pairing is only
valid while `MODULE.bazel` (coupled to this exact two-repo composition), `//:Cargo.toml`,
`//:Cargo.lock` and the `acme-case-rs`/`acme-codec-rs` manifests in `POLYGLOT_REPOS` stay
byte-identical to what produced it. Bazel's own `--lockfile_mode=update` default degrades a
mismatch SILENTLY back to today's splice-on-every-run behaviour rather than erroring — which
means the fixture can only ever make this test flake LESS, never introduce a new failure mode —
but silent is also how this exact defect went unnoticed for three incidents. Regenerate by
running this test once with network available and recapturing
`<integration-checkout>/MODULE.bazel.lock`.

**What was deliberately NOT done: adding `lockfile=` to `rust.py`'s `crate.from_cargo` tag.**
That is the structurally-correct fix for PRODUCTION (research-38 Q1.4's `maven_install.json`
analogue) but it needs a `cargo-bazel-lock.json` producer this harness does not have — `rust.py`
declares no Cargo `resolution()` step, so there is nothing to point `lockfile=` at without a new
design (an ADR), which is out of this task's scope. Fixing only the test's hermeticity does not
require it: `repin` staying unconditionally `True` is exactly why the replay-vs-splice branch
above is worth guarding at all.
"""


_CARGO_SPLICE_MARKERS: tuple[str, ...] = ("Updating crates.io index", "Splicing Cargo workspace")
"""The two literal lines `cargo-bazel splice` prints before it opens a socket, verbatim from the
reproduced flake (research-38 Q1.1: `$Q/run4.err:22`). Either one, anywhere in a real Bazel
invocation's output, means `crate.from_cargo` re-ran the splicer instead of replaying
`MODULE.bazel.lock` — i.e. that `RUST_MODULE_LOCK_FIXTURE` went stale and this run reached
`index.crates.io` live."""


def _assert_no_cargo_splice(fleet: Path) -> None:  # noqa: F811  (`fleet` is the imported fixture)
    """No bazel invocation this run made printed a cargo-splice marker (see above).

    Reads the FULL stdout/stderr `util/proc.py`'s `LoggedRunner` writes under
    `<fleet>/artifacts/logs/` — not `attempts.stderr_tail`, which `LOG_TAIL_BYTES` truncates to
    the last 32 KiB (research-38 Q3) and could scroll `Updating crates.io index` (one of the
    FIRST lines a splice prints) out of, on a build whose log grows past that window. This is the
    proof the network dependency is gone: not "the build was green," which a working network
    would also produce, but "no process this run ran ever printed the line cargo prints before it
    contacts crates.io."
    """
    logs = sorted((fleet / "artifacts" / "logs").rglob("*.std*.log"))
    assert logs, "no bazel invocation logged anything at all — the build never ran"
    hits = [
        (log, marker)
        for log in logs
        for marker in _CARGO_SPLICE_MARKERS
        if marker in log.read_text(encoding="utf-8", errors="replace")
    ]
    assert not hits, (
        f"cargo spliced instead of replaying the seeded {MODULE_LOCK_PATH} — the fixture has "
        f"gone stale (regenerate tests/fixtures/rust/MODULE.bazel.lock, see its docstring above "
        f"RUST_MODULE_LOCK_FIXTURE): {hits}"
    )


@pytest.mark.integration
def test_two_rust_repos_in_one_wave_both_build(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    tmp_path: Path,
    bazel_cache_home: Path,
    bazel_registry: str,
    bazel_fetch_bazelrc: str,
    bazel_startup_argv: tuple[str, ...],
    bazel_output_user_root: Path,
) -> None:
    """Two Rust repos in ONE wave, two different crates.io dependencies, one monorepo that builds.

    The Rust half of what the two JS and two Python tests above assert, and the ecosystem that
    could not pass it until Phase 3 stopped cutting a build worktree per repo. `crate.from_cargo`
    splices ONE `//:Cargo.toml` workspace naming every contributing crate in `members`, and
    `cargo metadata` fails the WHOLE workspace — not just the offending member — when a `members`
    entry names a directory that is not there:

        error: failed to load manifest for workspace member `…/rust/acme-codec-rs`
        referenced by workspace at `…/Cargo.toml`
        Caused by: No such file or directory (os error 2)
        Error: Failed to generate lockfile

    That is what a per-repo snapshot produced: the fleet-wide root files are computed over every
    plan prepared so far, so the first Rust repo of the wave was handed a `members` list naming
    its sibling while its own worktree — cut at its own merge — did not have the sibling's
    directory yet. `fleet build` exited 7 with both Rust repos `REQUIRES_HUMAN_INTERVENTION` and
    the LAST Rust repo of the wave green. One snapshot per wave, cut after the wave's last
    ingest, is the fix; this is its end-to-end proof.

    **The verdict is Bazel's, not the harness's.** `fleet build`'s exit code is quoted into the
    failure detail rather than asserted, exactly as the JS and Python tests do it: the claim is
    that `bazel build //...` over the integration branch succeeds, because a monorepo whose root
    workspace names a member that cannot be loaded is not a monorepo that builds, whatever the
    harness reported. `hex` and `heck` are named by exactly one repo each, so the root
    `Cargo.lock` really does have two candidate contents, and `@crates//:hex` resolving at all
    means cargo extended the lock `rust/acme-case-rs` seeded (which never mentioned `hex`).

    **Non-vacuity, twice.** Both packages must really carry a `rust_library` — a `//...` over a
    tree in which an ecosystem silently emitted no targets is green about nothing — and the
    build must really leave an rlib per crate, which is `rustc` having compiled `src/lib.rs`
    with its `--extern` resolved rather than analysis having merely succeeded.

    **The FLEET is the two Rust repos, and the reason is the disk ceiling.** `fleet build` gives
    every repo its own Bazel output base, and a Rust output base carries a whole `rust_toolchain`
    (rustc, cargo and `rust-std`); with the other four fixture repos in too, this one test drove
    the session's peak Bazel state to 6.59 GiB against `conftest`'s 6 GiB ceiling — which fails
    the session, and raising it is not on offer. This used to be done with `--repo`, and ADR-0055
    is why it is not any more: `--repo` narrows DISPATCH and never the domain, so the fleet-wide
    root files would still describe the four repos nothing built, `npm_link_all_packages` at the
    monorepo root would still name `//ts/acme/lib`, and the `bazel build //...` below would fail
    on a package that no invocation was ever asked to generate. That is the harness being right
    and the fixture being wrong: a whole-branch claim belongs to a fleet whose whole branch was
    built. `only_repos` bounds the fleet instead, which costs nothing this test claims — both
    Rust repos are still ingested and planned together in one wave, `_fleet_support_files` still
    computes `//:Cargo.toml` over both plans, and `//...` is still the whole integration branch.
    The polyglot union is covered by `test_build_against_a_real_bazel` and, over a fleet that
    includes both Rust repos, by
    `test_every_dispatched_worktree_carries_every_dest_the_root_files_were_computed_over` — which
    needs no Bazel and therefore no disk. The fleet's per-repo output bases are reaped as soon as
    `fleet build` returns for the same reason: they are dead weight while the checkout below
    builds, and `conftest`'s `bazel_cache_home` would only reap them at teardown.

    **And the session-shared output base is reaped at the end, which is this test paying its own
    bill.** `bazel_output_user_root` is session-scoped: whatever this test fetches into it is
    then measured after every later real-Bazel test in the run. A `rust_toolchain` there is
    ~1.5 GiB that only this test needs, and leaving it put the whole suite's peak at 6.00 GiB
    against a 6.00 GiB ceiling — green, and one ruleset bump from red. Later tests re-fetch from
    `repos/`, the content-addressed cache `conftest` keeps on purpose, so this costs analysis
    time and no network.

    **What this does NOT prove.** Only the INTRA-wave case: both fixtures declare no internal
    dependency, so §3.1 step 7 puts them in one wave, and that is asserted rather than assumed.
    The CROSS-wave residue ADR-0053 disclosed — an earlier wave settling with root files computed
    over a smaller domain and never re-admitted — is closed by ADR-0055 rather than by anything
    here: the root files are computed once per RUN, before the first build, so every wave builds
    against the same maximal set. Real Bazel proof of THAT is
    `test_a_second_invocation_republishes_root_files_that_still_describe_the_whole_branch`'s
    territory and it is offline, so nothing below should be read as covering it.
    """
    only_repos(fleet, ["acme-codec-rs", "acme-case-rs"])
    # Seeded and COMMITTED before `real_build` for the same reason its own `.bazelrc` is: Phase 3
    # cuts its build worktree from the `integration` tip, so an untracked root file is not in the
    # tree Bazel ever sees. This is what turns the very first `crate.from_cargo` evaluation of
    # this fresh monorepo into a lockfile REPLAY instead of a splice against live crates.io — see
    # `RUST_MODULE_LOCK_FIXTURE`'s docstring for the mechanism and `_assert_no_cargo_splice` below
    # for the guard that catches it silently going stale.
    (monorepo / MODULE_LOCK_PATH).write_text(
        RUST_MODULE_LOCK_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    git(monorepo, "add", "-A")
    git(monorepo, "commit", "-m", "seed MODULE.bazel.lock so crate.from_cargo replays, not splices")
    result = real_build(
        fleet,
        monorepo,
        bazel_cache_home,
        bazel_registry,
        bazel_fetch_bazelrc,
    )
    # The proof this test no longer depends on live crates.io: not "the build was green," which a
    # working network would also produce, but that no bazel invocation this run made ever printed
    # the line `cargo-bazel splice` prints before it opens a socket to `index.crates.io`.
    _assert_no_cargo_splice(fleet)
    assert wave_index(fleet, "acme-codec-rs") == wave_index(fleet, "acme-case-rs"), (
        "the two Rust repos landed in different waves, so this test would be exercising the "
        "CROSS-wave case, which one snapshot per wave does not address"
    )
    reap_bazel_state(bazel_cache_home)

    checkout = tmp_path / "integration-checkout"
    git(monorepo, "worktree", "add", "--detach", str(checkout), "integration")
    manifest = (checkout / "Cargo.toml").read_text(encoding="utf-8")
    detail = (
        f"fleet build exited {result.exit_code}\n\n"
        f"--- //:Cargo.toml ---\n{manifest}\n"
        f"--- //:Cargo.lock ---\n"
        f"{(checkout / 'Cargo.lock').read_text(encoding='utf-8')}\n"
    )
    for dest in RUST_DESTS:
        # The domain: the root workspace really names both members, so the tree below really has
        # to contain both — this is the fleet-wide fact the per-repo snapshot contradicted.
        assert f'"{dest}"' in manifest, detail
        body = (checkout / dest / "BUILD.bazel").read_text(encoding="utf-8")
        # Not vacuous: an ecosystem that stopped emitting targets would leave `//...` green over
        # a tree in which nothing is ever compiled.
        assert "rust_library(" in body, f"{dest} generated no rust_library\n{body}"

    built = subprocess.run(  # noqa: S603
        [*bazel_startup_argv, "build", "//..."],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
        timeout=1800.0,
    )
    detail += f"--- bazel stderr ---\n{built.stderr[-8000:]}"
    assert built.returncode == 0, detail
    assert "Build completed successfully" in built.stderr, detail
    for dest in RUST_DESTS:
        # `rustc` really ran: an rlib is the output of compiling the crate's own `src/lib.rs`
        # with its `--extern` resolved, which analysis alone cannot produce.
        rlibs = sorted((checkout / "bazel-bin" / dest).rglob("*.rlib"))
        assert rlibs, f"{dest} produced no rlib\n{detail}"
    # Asserted first, reaped after: the Rust toolchain this test fetched is dead weight in a
    # session-shared output base, and it is measured after every later test in the run.
    reap_bazel_state(bazel_output_user_root)
    bazel_output_user_root.mkdir(parents=True, exist_ok=True)


@pytest.mark.integration
@pytest.mark.skipif(
    shutil.which("bazel") is None,
    reason=(
        "bazel is not installed on this host; the whole point of this test is real Bazel's own "
        "answer about the monorepo the two JS repos merged into, and no fake can give it"
    ),
)
def test_two_js_repos_with_different_npm_dependencies_both_build(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    tmp_path: Path,
    bazel_cache_home: Path,
    bazel_registry: str,
    bazel_fetch_bazelrc: str,
    bazel_startup_argv: tuple[str, ...],
) -> None:
    """Two JS repos, two DIFFERENT external npm packages, one monorepo that has to build.

    **Why a second JS repo is the entire fixture change.** `npm_translate_lock` translates ONE
    lockfile for the whole workspace (`js.py:_PNPM_LOCK`), and every JS repo's `workspace_files()`
    declares that same root `//:pnpm-lock.yaml` — but the *content* it declared used to be that
    repo's own resolution. With one JS repo carrying external dependencies the two facts never
    met, so the design's single-lock assumption held vacuously through five checkpoints.
    `acme-report-ts` declares `ms` where `acme-app-ts` declares `left-pad`, which is the smallest
    fixture in which the fleet has two answers to "what is at `//:pnpm-lock.yaml`".

    **It was `xfail(strict=True)` and it is not any more.** The marker named the mechanism —
    `cli._module_inputs` unioning the fleet's root files with `support.setdefault(file.path,
    file)` over `sorted(plans)`, so the lower repo_id's lock won and the other repo's dependency
    vanished from the monorepo while `fleet build` still exited 0, because `acme-report-ts` built
    green in wave 0 and nothing re-admits a settled wave. ADR-0048 closed it: the root lock is
    now resolved once per ecosystem as a pnpm **workspace** with one importer per JS repo, the
    labels moved to `//<dest>:node_modules/<pkg>`, and the union raises instead of picking. The
    marker is deleted rather than left passing, exactly as `test_build_against_a_real_bazel`'s
    was, so this is now a guard: it turns red the moment the fleet goes back to one flat map.

    **This asserts the invariant, not the bug.** The claim is `bazel build //...` over the
    integration branch: a monorepo whose generated `deps` name a `node_modules/<pkg>` that its
    own root lock never resolved is not a monorepo that builds, whatever the harness reported. It
    is deliberately NOT written as "assert `ms` is present": pinning the symptom would go green
    on a fix that only changed *which* repo loses.

    `//...` and not the two JS patterns because the union is the point: the defect is invisible
    from inside either repo's own package.
    """
    add_repos(fleet, ["acme-report-ts"])
    result = real_build(fleet, monorepo, bazel_cache_home, bazel_registry, bazel_fetch_bazelrc)

    checkout = tmp_path / "integration-checkout"
    git(monorepo, "worktree", "add", "--detach", str(checkout), "integration")
    # Both repos really generated a package, so a green `//...` below cannot be green over a tree
    # in which one of them silently emitted nothing — which is the other way this could "pass".
    for dest in ("ts/acme/app", "ts/acme/report"):
        assert (checkout / dest / "BUILD.bazel").is_file(), f"{dest} generated no BUILD.bazel"
    built = subprocess.run(  # noqa: S603
        [*bazel_startup_argv, "build", "//..."],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
        timeout=1800.0,
    )
    # The lock is quoted into the failure so the report says WHAT the monorepo resolved, not only
    # that Bazel refused it: the harness exited 0 here, so the lock is the only evidence that the
    # two repos' resolutions were ever different.
    detail = (
        f"fleet build exited {result.exit_code}\n\n"
        f"--- //:pnpm-lock.yaml ---\n{(checkout / 'pnpm-lock.yaml').read_text(encoding='utf-8')}\n"
        f"--- bazel stderr ---\n{built.stderr[-8000:]}"
    )
    assert built.returncode == 0, detail
    assert "Build completed successfully" in built.stderr, detail


@pytest.mark.integration
def test_two_python_repos_with_different_pypi_dependencies_both_build(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    tmp_path: Path,
    bazel_cache_home: Path,
    bazel_registry: str,
    bazel_fetch_bazelrc: str,
    bazel_startup_argv: tuple[str, ...],
) -> None:
    """Two Python repos, two DIFFERENT PyPI distributions, one monorepo that has to build.

    The Python half of what `test_two_js_repos_with_different_npm_dependencies_both_build`
    asserts for npm, and it exists for the same reason: `pip.parse(requirements_lock =
    "//:requirements.lock")` builds ONE `@pypi` hub from ONE file at the monorepo root, and every
    Python unit's `workspace_files()` declares that same path. With a single Python repo carrying
    external dependencies the "one lock for the fleet" assumption held vacuously — `acme-app-py`
    and `acme-tool-py` both declare `requests`, so their contributions to the root lock were
    byte-identical however the harness combined them. `acme-metrics-py` declares `jinja2`, which
    is the smallest fixture in which the fleet has two different answers to "what is at
    `//:requirements.lock`".

    **Python is NOT pin-preserving and that is structural, not a harness choice.** A pip
    requirements file is a flat set with one `==` per distribution and `pip.parse` reads exactly
    one of them, so there is no per-importer construction to mirror pnpm's workspace with. What
    the fleet-wide resolve buys instead is that the single lock is a resolution of the UNION: `uv`
    sees every repo's specs at once and either finds one closure that satisfies all of them or
    fails loudly. Two repos that pinned genuinely incompatible ranges are a real conflict, and
    surfacing it as a `uv` error naming both specs is the honest outcome — silently resolving one
    repo's half is not.

    `//...` and not the two Python package patterns, for the reason the JS test gives: the union
    is the point, and the defect is invisible from inside either repo's own package. The lock is
    quoted into every failure because a monorepo whose `@pypi` hub is missing one repo's
    distribution is a tree Bazel refuses for a reason only the lock explains.
    """
    add_repos(fleet, ["acme-metrics-py"])
    result = real_build(fleet, monorepo, bazel_cache_home, bazel_registry, bazel_fetch_bazelrc)

    checkout = tmp_path / "integration-checkout"
    git(monorepo, "worktree", "add", "--detach", str(checkout), "integration")
    # Both repos really generated a package, so a green `//...` below cannot be green over a tree
    # in which one of them silently emitted nothing — which is the other way this could "pass".
    for dest in ("py/acme_app_py", "py/acme-metrics-py"):
        assert (checkout / dest / "BUILD.bazel").is_file(), f"{dest} generated no BUILD.bazel"
    lock = (checkout / "requirements.lock").read_text(encoding="utf-8")
    detail = f"fleet build exited {result.exit_code}\n\n--- //:requirements.lock ---\n{lock}\n"
    # The union, stated directly: one repo's direct dependency, the other's, and the transitive
    # distribution that can only be there because a resolver read `jinja2`'s own metadata. This is
    # asserted BEFORE Bazel because a lock missing half the fleet is the defect whether or not the
    # analysis phase happens to reach the package that needed it.
    for distribution in ("requests==", "certifi==", "jinja2==", "markupsafe=="):
        assert distribution.lower() in lock.lower(), (
            f"{distribution!r} is absent from the fleet-wide lock: the resolve was not over the "
            f"UNION of every Python unit's specs\n{detail}"
        )
    built = subprocess.run(  # noqa: S603
        [*bazel_startup_argv, "build", "//..."],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
        timeout=1800.0,
    )
    detail += f"--- bazel stderr ---\n{built.stderr[-8000:]}"
    assert built.returncode == 0, detail
    assert "Build completed successfully" in built.stderr, detail


@pytest.mark.integration
@pytest.mark.skipif(
    shutil.which("bazel") is None,
    reason=(
        "bazel is not installed on this host; this is D112's own proof, and it is exactly the "
        "one no `FakeBazel` invocation can give: that a real `bazel test` runs and PASSES a "
        "target built from a `test_srcs` list a real ecosystem adapter actually populated"
    ),
)
def test_a_python_test_target_runs_and_passes_under_a_real_bazel(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    tmp_path: Path,
    bazel_cache_home: Path,
    bazel_registry: str,
    bazel_fetch_bazelrc: str,
    bazel_startup_argv: tuple[str, ...],
) -> None:
    """D112, closed end to end: `bazel test //py/acme-widgets-py:acme-widgets-py_test` PASSES
    against the real toolchain, over a target `fleet build` emitted from a real `test_srcs` list.

    **Why this and not just the `FakeBazel` version above.** `docs/INTEGRATION_HONESTY.md`'s
    `## D112` entry's whole point is that no real-Bazel path had EVER produced a nonzero test
    target — `test_build_against_a_real_bazel`'s own `bazel build //...` never runs `bazel test`
    at all, and every existing real-Bazel test in this file asks `build`, never `test`. A
    `FakeBazel` seam answering from a canned table cannot show a test binary actually executing
    and exiting 0; only a real `bazel test` invocation, over a real Python interpreter running
    the real `assert double(21) == 42`-shaped script, can.

    Run alongside the fast unit-tier test above (same file, `@pytest.mark.integration`, skipped
    together) rather than in it, per this task's brief: a real Bazel invocation is too slow for
    the fast tier.
    """
    add_repos(fleet, ["acme-widgets-py"])
    result = real_build(fleet, monorepo, bazel_cache_home, bazel_registry, bazel_fetch_bazelrc)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    checkout = tmp_path / "integration-checkout"
    git(monorepo, "worktree", "add", "--detach", str(checkout), "integration")
    dest = "py/acme-widgets-py"
    build_file = checkout / dest / "BUILD.bazel"
    assert build_file.is_file(), f"{dest} generated no BUILD.bazel"
    body = build_file.read_text(encoding="utf-8")
    assert "py_test(" in body, body

    tested = subprocess.run(  # noqa: S603
        [*bazel_startup_argv, "test", f"//{dest}:acme-widgets-py_test", "--test_output=errors"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
        timeout=1800.0,
    )
    detail = (
        f"fleet build exited {result.exit_code}\n\n--- bazel test stderr ---\n"
        f"{tested.stderr[-8000:]}"
    )
    # `--test_output=errors` prints nothing about a PASSING test but the failing test's own
    # output on a red one, so the exit code plus Bazel's own "completed successfully" summary —
    # not a `PASSED`/`FAILED` string that only appears under `--test_output=all` or in the test
    # log — is what a passing `bazel test` actually asserts about itself. A failing target makes
    # `bazel test`'s OWN exit nonzero and its summary read "FAILED", which this would catch.
    assert tested.returncode == 0, detail
    assert f"//{dest}:acme-widgets-py_test" in tested.stderr, detail
    assert "Build completed successfully" in tested.stderr, detail
    log = checkout / "bazel-testlogs" / dest / "acme-widgets-py_test" / "test.log"
    assert log.is_file(), f"{detail}\n(no test.log at {log})"
    assert "Traceback" not in log.read_text(encoding="utf-8"), log.read_text(encoding="utf-8")


@pytest.mark.integration
@pytest.mark.skipif(
    shutil.which("bazel") is None,
    reason=(
        "bazel is not installed on this host; without it the last assertion below — that real "
        "Bazel builds the Python package out of the resolved hub — cannot run, and a lockfile "
        "nobody fed to a ruleset proves only that a resolver wrote a file"
    ),
)
def test_the_resolved_lock_carries_the_transitive_closure(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel_cache_home: Path,
    bazel_registry: str,
    bazel_fetch_bazelrc: str,
    bazel_startup_argv: tuple[str, ...],
) -> None:
    """The lockfiles committed onto `integration` by a REAL `fleet build` are resolutions:
    `certifi` is in `requirements.lock`, `left-pad` is in a `packages:` section of
    `pnpm-lock.yaml` — and real Bazel builds the Python package out of the hub they produce.

    **`certifi` is the assertion, and it is chosen because nothing in this fleet declares it.**
    `acme-app-py`'s `pyproject.toml` says `requests>=2.31` and no manifest anywhere names
    `certifi`, `charset-normalizer`, `idna` or `urllib3`. They can only appear by a resolver
    having read `requests`' own metadata — which is exactly what `pip.parse` does at build time,
    and exactly what it could not find when the "lock" was the declared spec list. So a lock that
    contains `certifi` cannot have been synthesized from anything this harness knew, and a lock
    that does not is the defect no matter how plausible it looks.

    The Bazel step at the end is what makes the file assertions mean something. A lockfile is
    consumed by a ruleset, not by a test: `pip.parse` builds one repository per resolved
    distribution and then asks the hub for each dependency's dependencies, and the only thing
    that can answer "the closure is complete" is the ruleset doing that walk. This is asserted on
    `bazel build`'s own exit status over the generated tree, run out of the worktree Phase 3
    built, and not on any harness-internal state.
    """
    result = real_build(fleet, monorepo, bazel_cache_home, bazel_registry, bazel_fetch_bazelrc)
    # Not pinned to exit 0: `test_build_against_a_real_bazel` owns the fleet-wide verdict and
    # names what is still open. This test is about the lockfiles and the hub they build.
    assert result.exit_code in {
        ExitCode.SUCCESS,
        ExitCode.REQUIRES_HUMAN_INTERVENTION,
    }, result.output

    requirements = root_file(monorepo, "requirements.lock")
    assert "certifi==" in requirements, (
        "no manifest in this fleet names `certifi`; it is in the lock only if a resolver read "
        "`requests`' metadata, and its absence is the defect this test exists for\n"
        f"{requirements}"
    )
    assert re.search(r"^requests==\d", requirements, flags=re.MULTILINE), requirements
    assert "requests>=2.31" not in requirements, "a spec is not a resolution"

    pnpm = root_file(monorepo, "pnpm-lock.yaml")
    assert "\npackages:\n" in pnpm, f"an @npm hub built from this declares nothing\n{pnpm}"
    assert "left-pad@1.3.0" in pnpm, pnpm
    assert "GENERATED BY fleet" not in pnpm, "the floor, not a resolution"

    # …and the ruleset agrees. `//py/acme_app_py:acme_app_py` depends on `@pypi//requests`, whose
    # own `//:pkg` names `certifi`, `charset_normalizer`, `idna` and `urllib3` inside the hub —
    # the four packages whose absence was reported as "no such package
    # '@@rules_python++pip+pypi//certifi'". Building it is the only check that walks that far.
    worktree = build_worktree(fleet, DEPENDENT_REPO)
    built = subprocess.run(  # noqa: S603
        [*bazel_startup_argv, "build", f"//{DESTINATIONS[DEPENDENT_REPO]}/..."],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
        timeout=1800.0,
    )
    assert built.returncode == 0, built.stderr[-6000:]
    assert "Build completed successfully" in built.stderr, built.stderr[-6000:]


@pytest.mark.integration
def test_re_resolving_the_same_specs_produces_a_byte_identical_lock() -> None:
    """The real resolvers, run twice over the same declared inputs, write the same bytes.

    **Why:** a resolution is an input to the run digest's world — the lock is committed onto
    `integration` and every later wave, every `fleet resume` and Phase 4 build from it. A
    resolver whose output moved between two runs of the same fleet would rewrite the monorepo
    root with no diff anywhere in this repository, which is the same reproducibility hole an
    unpinned `bazel_dep` is (§9, §11.6). `uv`'s `--no-header` is here because of exactly this:
    its default banner embeds the absolute output path, which is a per-run scratch directory.

    What this does NOT claim is that a resolution is stable across *time*: both resolvers ask a
    live index, so a new upstream release changes the answer. That is a property of the ecosystem
    and the reason the lock is committed rather than recomputed — it is the record.
    """
    ecosystems.discover()
    units = {
        Ecosystem.PYPI: BuildUnit(
            unit_id="acme-app-py",
            ecosystem=Ecosystem.PYPI,
            dest="py/acme_app_py",
            external_coordinates=[
                Coordinate(ecosystem=Ecosystem.PYPI, name="requests", version_spec=">=2.31")
            ],
        ),
        Ecosystem.NPM: BuildUnit(
            unit_id="acme-app-ts",
            ecosystem=Ecosystem.NPM,
            dest="ts/acme/app",
            external_coordinates=[
                Coordinate(ecosystem=Ecosystem.NPM, name="left-pad", version_spec="^1.3.0")
            ],
        ),
    }
    for eco, unit in units.items():
        adapter = ecosystems.for_ecosystem(eco)
        plan = adapter.resolution([unit])
        assert plan is not None, eco
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            locks = [
                asyncio.run(
                    cli._run_resolution(
                        plan,
                        repo_id=str(unit.unit_id),
                        # No tree to carry from: the resolve under test is the SYNTHESIZED one,
                        # and it is a sequence now because the ecosystem's carry candidates are
                        # searched across every one of its units' worktrees (ADR-0048).
                        worktrees=[root / "empty"],
                        scratch=root / f"run-{attempt}",
                    )
                )
                for attempt in (1, 2)
            ]
        assert locks[0] == locks[1], f"{eco.value}: {plan.argv[0]} is not deterministic"
        assert locks[0].strip(), f"{eco.value}: an empty lock is not a resolution"


#: The one internal dependency edge in the fixture fleet that both ends of survive Phase 3 today:
#: `acme-app-py`'s `pyproject.toml` declares `acme-lib-py>=2.0`, Phase 1 records it as an internal
#: `DECLARED_DEP`, and the generated `py_library` for the app carries `//py/acme_lib_py:acme_lib_py`
#: in `deps`. Named as a pair so the ordering test below reads as a statement about a dependency
#: and its dependent rather than about two strings.
DEPENDENCY_REPO: str = "acme-lib-py"
DEPENDENT_REPO: str = "acme-app-py"


def wave_index(root: Path, repo_id: str) -> int:
    """The wave §3.1 step 7 assigned this repo, read from `wave_members`."""
    rows = query(
        root,
        "SELECT wave_index FROM wave_members WHERE node_kind = 'REPO' AND node_id = ?",
        (repo_id,),
    )
    assert len(rows) == 1, (repo_id, rows)
    return int(rows[0][0])


def build_snapshot_ref(root: Path, repo_id: str) -> str:
    """The immutable `refs/fleet/<run>/integration/<seq>` this repo's Phase 3 built against.

    Read off the persisted `attempts` row rather than recomputed, because the row is what §3.3
    promises a `BUILD_ERROR` is a property of: if the ref on the row is not the tree that was
    analysed, every failure in this suite is attributed to the wrong commit.
    """
    refs = {row["integration_ref"] for row in attempts(root, 3) if row["repo_id"] == repo_id}
    assert len(refs) == 1, (repo_id, refs)
    ref = refs.pop()
    assert isinstance(ref, str) and ref, (repo_id, ref)
    return ref


def added_in(monorepo: Path, path: str) -> str:
    """The commit on `integration` that ADDED `path`. Exactly one, or the assertion says so."""
    shas = git(
        monorepo, "log", "--diff-filter=A", "--format=%H", "integration", "--", path
    ).split()
    assert len(shas) == 1, (path, shas)
    return shas[0]


@pytest.mark.integration
@pytest.mark.skipif(
    shutil.which("bazel") is None,
    reason=(
        "bazel is not installed on this host; without it the last assertion below — that real "
        "Bazel analyses the dependency's package out of the DEPENDENT's tree — cannot run, and "
        "the git-level ordering assertions alone would not prove the tree is buildable"
    ),
)
def test_a_dependencys_generated_package_is_on_the_branch_before_its_dependents_snapshot(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel_cache_home: Path,
    bazel_registry: str,
    bazel_fetch_bazelrc: str,
    bazel_startup_argv: tuple[str, ...],
) -> None:
    """**D5's ordering guarantee, pinned.** A dependency's generated `BUILD.bazel` is merged onto
    `integration` before the immutable snapshot its dependent builds against is cut.

    D5 was reported as "publication is gated on the repo's own Phase 3 succeeding, so the
    dependent analyses a tree with no `py/acme_lib_py/BUILD.bazel`". The gate is real and is not a
    defect — publishing a `BUILD.bazel` that does not build would put an unbuildable package on
    the branch for every later wave. What made the gate *fatal* was D9: `acme-lib-py` is a library
    with no tests, `bazel test` exits 4 ("No test targets were found"), `classify_build_failure`
    called that a retryable test failure, and the repo escalated without ever reaching its publish
    step. D5 was a cascade, and closing D9 closed it — which is precisely why it needs a test that
    fails for the ORDERING rather than for the cascade, or the next regression in either one is
    invisible.

    Three claims, each of which the defect violated and none of which a fake can satisfy:

    1. **wave order** — the dependency migrates strictly earlier than its dependent (§3.1 step 7).
       If they shared a wave nothing would order them and the rest would be luck.
    2. **publish-before-snapshot** — the commit that ADDED `py/acme_lib_py/BUILD.bazel` is an
       ancestor of the sha the dependent's `attempts.integration_ref` names, and is NOT an
       ancestor of the dependency's own snapshot. The second half is what makes the first mean
       something: a file that was on the branch all along would satisfy an ancestry check
       vacuously, exactly the way the double-relocation bug satisfied its own prefix guard.
    3. **real Bazel agrees** — `bazel build //py/acme_lib_py/...` run inside the DEPENDENT's build
       worktree exits 0. Not a path check: it is the analysis that reported `no such package
       'py/acme_lib_py': BUILD file not found` when the file was missing, run over the same tree
       against which that error was produced.
    """
    result = real_build(fleet, monorepo, bazel_cache_home, bazel_registry, bazel_fetch_bazelrc)
    # This test is about the ORDER things reached the branch in, not about the fleet being green:
    # `test_build_against_a_real_bazel` above is what asserts exit 0, and duplicating that here
    # would make this one red for every future defect in any repo while saying nothing new about
    # ordering. The three assertions below are what it exists for and none of them reads the exit
    # code.
    assert result.exit_code in {
        ExitCode.SUCCESS,
        ExitCode.REQUIRES_HUMAN_INTERVENTION,
    }, result.output

    # 1. the sequencer really ordered them, and the dependency is the earlier one.
    assert wave_index(fleet, DEPENDENCY_REPO) < wave_index(fleet, DEPENDENT_REPO), (
        f"{DEPENDENT_REPO} must migrate in a strictly later wave than {DEPENDENCY_REPO}; in one "
        "wave nothing orders them and the publish below is a race"
    )

    # 2. the publish merge really landed between the two snapshots.
    published = added_in(monorepo, f"{DESTINATIONS[DEPENDENCY_REPO]}/BUILD.bazel")
    dependent_ref = build_snapshot_ref(fleet, DEPENDENT_REPO)
    dependency_ref = build_snapshot_ref(fleet, DEPENDENCY_REPO)
    assert published in git(monorepo, "rev-list", dependent_ref).split(), (
        f"{DEPENDENT_REPO} built against {dependent_ref}, which does not contain the commit that "
        f"published {DESTINATIONS[DEPENDENCY_REPO]}/BUILD.bazel — this is D5"
    )
    assert published not in git(monorepo, "rev-list", dependency_ref).split(), (
        "the generated BUILD.bazel was already on the branch before its own repo built, so the "
        "ancestry assertion above is vacuous and proves no ordering at all"
    )
    assert f"{DESTINATIONS[DEPENDENCY_REPO]}/BUILD.bazel" in git(
        monorepo, "ls-tree", "-r", "--name-only", dependent_ref
    ).split()

    # 3. and real Bazel can analyse the dependency's package out of the dependent's own tree.
    worktree = build_worktree(fleet, DEPENDENT_REPO)
    assert (worktree / f"{DESTINATIONS[DEPENDENCY_REPO]}/BUILD.bazel").is_file(), worktree
    analysed = subprocess.run(  # noqa: S603
        [*bazel_startup_argv, "build", f"//{DESTINATIONS[DEPENDENCY_REPO]}/..."],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
        timeout=1800.0,
    )
    assert analysed.returncode == 0, analysed.stderr[-6000:]
    assert "Build completed successfully" in analysed.stderr, analysed.stderr[-6000:]


def ingested_paths(monorepo: Path, repo_id: str) -> set[str]:
    """Every path in the tree of `repo_id`'s ingested history, read off the merge's SECOND parent.

    `<merge>^2` is the rewritten source history itself. Its tip tree is what the integration
    branch actually gained from this repo, so a set comparison against it is a comparison against
    the thing the build will be run over.
    """
    merges = git(
        monorepo, "log", "--format=%H", f"--grep=Source-Repo: {repo_id}", "integration"
    ).split()
    assert len(merges) == 1, (repo_id, merges)
    return set(git(monorepo, "ls-tree", "-r", "--name-only", f"{merges[0]}^2").split())


def expected_paths(repo_id: str) -> set[str]:
    """The EXACT path set §3.3 step 1 owes for one fixture repo: its files, each once, at `<dest>`.

    Derived from the fixture's own file list rather than restated, so a fixture that grows a file
    cannot silently stop being checked.
    """
    dest = DESTINATIONS[repo_id]
    return {f"{dest}/{name}" for name in FIXTURE_REPOS[repo_id]}


@pytest.mark.integration
@pytest.mark.skipif(
    shutil.which("git-filter-repo") is None,
    reason=(
        "git-filter-repo is not installed on this host; the double relocation is invisible "
        "through cli.FILTER_REPO_RUNNER, which never moves a path"
    ),
)
def test_ingest_does_not_relocate_an_already_relocated_tree(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolver: FakeResolver,
) -> None:
    """The ingested tree is EXACTLY `<dest>/<the repo's files>` — asserted as a set, not a prefix.

    This test found defect D3 and is the reason the assertion is written this way. Phase 2 moves
    the tree into `<dest>/` by committing renames; Phase 3 then re-rooted that already-rooted
    history and every path landed at `<dest>/<dest>/…` (observed:
    `py/acme_lib_py/py/acme_lib_py/pyproject.toml`). Nothing in the harness noticed — the state
    rows were green, the merge was real, the provenance trailers were correct — and the first
    signal was a Bazel analysis error.

    **A prefix assertion cannot express this.** `py/acme_lib_py/py/acme_lib_py/pyproject.toml`
    does start with `py/acme_lib_py/`, so `startswith(f"{dest}/")` is *vacuously satisfied by the
    bug* — which is exactly what let it survive. `== expected_paths(repo_id)` is satisfied by one
    tree only: the doubled path is not in the expected set and the un-doubled path is missing from
    the actual set, so the bug fails on both halves of the comparison.
    """
    monkeypatch.setattr(cli, "BAZEL_RUNNER", FakeBazel(fleet / "artifacts" / "fake-bazel"))
    transformed(fleet)
    assert build(fleet, "--no-sandbox").exit_code == ExitCode.SUCCESS

    for repo_id in DESTINATIONS:
        assert ingested_paths(monorepo, repo_id) == expected_paths(repo_id), repo_id


@pytest.mark.integration
@pytest.mark.skipif(
    shutil.which("git-filter-repo") is None,
    reason=(
        "git-filter-repo is not installed on this host; the double relocation is invisible "
        "through cli.FILTER_REPO_RUNNER, which never moves a path"
    ),
)
def test_re_running_the_real_ingest_lands_the_same_tree_and_no_second_merge(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolver: FakeResolver,
) -> None:
    """ADR-0014 retries Phase 3, so the fixed relocation has to survive being run again.

    The failure this guards against is the one `vcs/filter_repo.py` already documents for a
    *directory*: `--force` turns git-filter-repo's refusal of an already-filtered repo into a
    silent second `--path-rename`, nesting the destination. `_ingest_build_source` re-clones
    instead, and `ingest()` finds the existing `Source-Repo`/`Source-Sha` merge — so the second
    run must add no merge commit and leave a byte-identical path set, not merely a "green" exit
    code.
    """
    monkeypatch.setattr(cli, "BAZEL_RUNNER", FakeBazel(fleet / "artifacts" / "fake-bazel"))
    transformed(fleet)
    assert build(fleet, "--no-sandbox").exit_code == ExitCode.SUCCESS
    first = {repo_id: ingested_paths(monorepo, repo_id) for repo_id in DESTINATIONS}
    merge_count = len(git(monorepo, "rev-list", "--merges", "integration").split())

    assert build(fleet, "--no-sandbox").exit_code == ExitCode.SUCCESS

    assert len(git(monorepo, "rev-list", "--merges", "integration").split()) == merge_count
    for repo_id in DESTINATIONS:
        assert ingested_paths(monorepo, repo_id) == expected_paths(repo_id), repo_id
        assert ingested_paths(monorepo, repo_id) == first[repo_id], repo_id


@pytest.mark.integration
@pytest.mark.skipif(
    shutil.which("git-filter-repo") is None,
    reason=(
        "git-filter-repo is not installed on this host; §3.3 step 1's rewrite of the commits "
        "BEHIND the branch tip is therefore unproven here — every other test drives it through "
        "cli.FILTER_REPO_RUNNER, which proves the argv and nothing about the rewritten history"
    ),
)
def test_ingest_rewrites_history_not_only_the_tip(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolver: FakeResolver,
) -> None:
    """Every commit of the ingested history — not just the tip — touches only `<dest>/`.

    That is the property the fake cannot have: Phase 2 relocated the *tree* by committing
    renames, so the tip is right either way, and only a real `git-filter-repo` moves the history
    behind it — which is what makes `git log --follow` work across the migration boundary.

    `FILTER_REPO_RUNNER` is left `None` here so `cli._ingest_one` shells out to the real binary;
    only `BAZEL_RUNNER` is faked, because this test is about the ingest, not about the build.

    The walk is over the merge commit's SECOND parent, deliberately. `git log --name-only` prints
    no paths at all for a merge commit (git suppresses merge diffs by default), so the obvious
    `--grep=Source-Repo:` one-liner reports an empty path list and an `all(...)` over an empty
    list is vacuously true — a green assertion proving nothing. `<merge>^2` is the rewritten
    source history itself, which is exactly the thing under test.
    """
    monkeypatch.setattr(cli, "BAZEL_RUNNER", FakeBazel(fleet / "artifacts" / "fake-bazel"))
    assert cli.FILTER_REPO_RUNNER is None, "the seam must be absent for this test to mean anything"
    transformed(fleet)
    assert build(fleet, "--no-sandbox").exit_code == ExitCode.SUCCESS

    for repo_id in DESTINATIONS:
        merges = git(
            monorepo, "log", "--format=%H", f"--grep=Source-Repo: {repo_id}", "integration"
        ).split()
        assert len(merges) == 1, (repo_id, merges)
        listed = git(monorepo, "log", "--name-only", "--format=", f"{merges[0]}^2")
        paths = {line for line in listed.splitlines() if line.strip()}
        assert paths, f"{repo_id}: the ingested history carries no paths at all"
        # The EXACT set, not a prefix: `<dest>/<dest>/x` starts with `<dest>/` too, so a
        # `startswith` assertion here was vacuously satisfied by defect D3 for as long as it
        # was live. Every path any commit of the rewritten history touches must be one of the
        # repo's files at its destination — no extras, and nothing left behind at the root.
        assert paths == expected_paths(repo_id), (repo_id, sorted(paths))
        # The commit BEHIND the tip is the one a copy-the-tree ingest cannot have relocated.
        root = git(monorepo, "rev-list", "--max-parents=0", f"{merges[0]}^2").split()[-1]
        root_tree = set(git(monorepo, "ls-tree", "-r", "--name-only", root).split())
        assert root_tree == expected_paths(repo_id), (repo_id, sorted(root_tree))


def test_sqlite_is_readable_after_the_two_phases(
    fleet: Path, monorepo: Path, bazel: FakeBazel  # noqa: F811
) -> None:
    """The `attempts` rows §3.4 step 3 says the report is assembled FROM are really there, with
    the columns §6 requires of a build row.

    A report assembled in memory and never persisted is a report a crash erases, and §3.3's
    "each attempt is one `attempts` row with `command`, `exit_code`, `duration_ms` and truncated
    output" is the only durable record of what this harness executed.
    """
    transformed(fleet)
    assert build(fleet, "--no-sandbox").exit_code == ExitCode.SUCCESS
    assert verify(fleet, "--rdeps-limit", "3").exit_code == ExitCode.SUCCESS

    conn = sqlite3.connect(fleet / "state" / "fleet.db")
    try:
        rows = conn.execute(
            "SELECT phase, command, exit_code, duration_ms, integration_ref FROM attempts "
            " WHERE phase IN (3, 4)"
        ).fetchall()
    finally:
        conn.close()
    assert rows, "no build/verify attempts were persisted"
    for phase, command, exit_code, duration_ms, ref in rows:
        argv = json.loads(str(command))
        assert argv and argv[0] in ("bazel", "docker"), argv
        assert exit_code is not None, argv
        assert duration_ms >= 0
        assert str(ref).startswith("refs/fleet/"), (phase, ref)


# --------------------------------------------------------------------------------------
# 7½. §12.31 case (ii), Leg C2 (round VI task 66) — a real Phase-3 build failure attributed
# to a broken hoist
# --------------------------------------------------------------------------------------
# Through the fakes, same as sections 1-6 above and for the same stated reason (this file's own
# docstring): "what is real about them is the argv, the exit-code handling, the output parsing,
# the failure classification and everything downstream" — exactly this task's scope. Whether REAL
# bazel really spells its label-resolution errors this way is answered independently and
# separately, at the unit level, against five real quoted strings already in this codebase's own
# adapter docstrings (`test_workers_build.py::
# test_hoist_broke_owner_matcher_reads_real_quoted_bazel_error_forms`) — this test's job is only
# to prove the WIRING: a real `contracts` row, a real failing `fleet build` dispatch, and a real
# database left with `status='FAILED'`, a `HoistBrokeOwner` finding, and `phases.attempts`
# untouched. Building a REAL bazel package at `hoist_target_path` is a separate, much larger lift
# this task does not need: nothing in `src/fleet` today (a disclosed, pre-existing D113/ADR-0119
# scope boundary — "narrow read-only PASS 2b, nothing commits hoisted contract content") ever
# populates `BuildUnit.contract_deps`, so no organic manifest-driven dependency on a hoisted
# contract's package exists to fail against for real; the `contracts` row is seeded directly,
# which is this file's own established convention for state a phase upstream of the one under
# test does not itself organically produce (see e.g. the direct `INSERT INTO stubs` above).

_HOIST_BREAK_CONTRACT_ID = "openapi:acme.shared"
_HOIST_BREAK_TARGET_PATH = "contracts/openapi/acme-shared"
_HOIST_BREAK_STDERR = (
    "ERROR: /work/ts/acme/app/BUILD.bazel:5:12: no such target "
    "'//contracts/openapi/acme-shared:pkg': target 'pkg' not declared in package "
    "'contracts/openapi/acme-shared'\n"
)


def _bazel_seam_failing_one_dest(log_root: Path, *, fail_dest: str, fail_stderr: str) -> Any:
    """A minimal `cli.BAZEL_RUNNER` seam: every `bazel` call succeeds except `bazel build` for
    `fail_dest`, which exits 1 with `fail_stderr` written to a real log file (so `WorkerError.
    artifact_ref` -- always the FULL stream, never `stderr_tail` -- names a real path this test's
    matcher assertion can read back). Deliberately narrower than the shared `FakeBazel` above
    (which hardcodes one fixed `LOUD_STDERR` for every failure): this test needs a SPECIFIC quoted
    label, and adding that to the shared class would widen a fixture ~50 other tests share for a
    need only this one has.
    """
    calls = {"n": 0}
    log_root.mkdir(parents=True, exist_ok=True)  # once, here -- `mkdir` inside `async def runner`
    # below is ASYNC240 (a blocking pathlib call in an async function); made once at setup time,
    # synchronously, in this plain `def` factory, mirrors `FakeBazel._result`'s own split above
    # (a sync helper doing the file I/O, called from the async `__call__`).

    async def runner(
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        deadline: float | None = None,
        timeout_s: float | None = None,
    ) -> ProcResult:
        _ = (env, deadline, timeout_s)
        calls["n"] += 1
        out_path = log_root / f"call-{calls['n']}.out"
        err_path = log_root / f"call-{calls['n']}.err"
        pattern = next((a for a in argv if a.startswith("//")), "")
        dest = pattern.removeprefix("//").removesuffix("/...")
        sub = argv[1] if len(argv) > 1 else ""
        fail = sub == "build" and dest == fail_dest
        text = fail_stderr if fail else ""
        out_path.write_text("", encoding="utf-8")
        err_path.write_text(text, encoding="utf-8")
        return ProcResult(
            argv=tuple(argv),
            exit_code=1 if fail else 0,
            stdout_tail="",
            stderr_tail=text,
            duration_ms=5,
            timed_out=False,
            stdout_bytes=0,
            stderr_bytes=len(text.encode()),
            stdout_path=out_path,
            stderr_path=err_path,
            cwd=cwd,
        )

    return runner


def test_a_real_build_failure_naming_a_hoisted_contracts_package_is_attributed_and_terminal(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    filter_repo: FakeFilterRepo,
    resolver: FakeResolver,
    gazelle: FakeGazelle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§12.31 case (ii), Leg C2 (round VI task 66, ADR-0123): a `bazel build` failure whose
    stderr names a watched hoist's package writes `contracts.status='FAILED'`, one
    `HoistBrokeOwner` finding, and spends `acme-app-ts`'s Phase 3 `phases.attempts` ZERO rungs --
    the assertion that actually proves the `retryable=False` wiring worked (CLAUDE.md Rule 12:
    "not merely that the string match fired").

    `acme-app-ts` is chosen as the failing repo deliberately: it is also given `owning_repo_id`
    on the seeded contract, i.e. this exercises SPEC's FIRST-named disjunct -- the owner's own
    build -- which `graph/cycles.py::_materialize` structurally excludes from ever holding a
    `CONTRACT_CONSUME` edge to its own contract (research-38-report.md Question 1). An edge- or
    wave-membership-based attribution could never have caught this case; the plain string match
    against `hoist_target_path` does.
    """
    _ = (filter_repo, resolver, gazelle)
    transformed(fleet)

    fail_dest = DESTINATIONS["acme-app-ts"]
    run_id = query(fleet, "SELECT run_id FROM runs")[0][0]
    assert query(
        fleet, "SELECT 1 FROM phases WHERE repo_id = ? AND phase = 3", ("acme-app-ts",)
    ) == [], "sanity: Phase 3 has not even been admitted yet, so no attempt could have been spent"

    conn = sqlite3.connect(fleet / "state" / "fleet.db", isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO contracts (run_id, contract_id, kind, identifier, owning_repo_id, "
            "                       extractable, hoist_target_path, status, detected_at) "
            "VALUES (?, ?, 'OPENAPI', 'acme.shared', ?, 1, ?, 'HOISTED', ?)",
            (
                run_id,
                _HOIST_BREAK_CONTRACT_ID,
                "acme-app-ts",
                _HOIST_BREAK_TARGET_PATH,
                "2026-09-06T00:00:00+00:00",
            ),
        )
    finally:
        conn.close()

    fake_bazel = _bazel_seam_failing_one_dest(
        fleet / "artifacts" / "fake-bazel-hoist-break",
        fail_dest=fail_dest,
        fail_stderr=_HOIST_BREAK_STDERR,
    )
    monkeypatch.setattr(cli, "BAZEL_RUNNER", fake_bazel)
    build(fleet, "--no-sandbox")  # exit code not asserted: one repo of four is meant to fail

    assert query(
        fleet, "SELECT status FROM contracts WHERE contract_id = ?", (_HOIST_BREAK_CONTRACT_ID,)
    ) == [("FAILED",)]

    findings = query(
        fleet,
        "SELECT repo_id, severity, payload FROM findings WHERE kind = 'HoistBrokeOwner'",
    )
    assert len(findings) == 1, findings
    finding_repo_id, severity, raw_payload = findings[0]
    assert finding_repo_id == "acme-app-ts"
    assert severity == "error"
    finding_payload = json.loads(str(raw_payload))
    assert finding_payload["contract_id"] == _HOIST_BREAK_CONTRACT_ID
    assert finding_payload["hoist_target_path"] == _HOIST_BREAK_TARGET_PATH
    assert finding_payload["repo_id"] == "acme-app-ts"
    assert "no such target" in finding_payload["matched_line"]

    after = query(
        fleet,
        "SELECT status, attempts FROM phases WHERE repo_id = ? AND phase = 3",
        ("acme-app-ts",),
    )
    assert after == [("REQUIRES_HUMAN_INTERVENTION", 0)], (
        "a non-retryable BUILD_ERROR must terminate without spending a rung (PhaseRunner."
        "_terminate_uncharged) -- this is the assertion that proves retryable=False actually "
        "reached the ladder, not merely that the string match fired"
    )


# --------------------------------------------------------------------------------------
# 8. the REAL BUILD-file generator
# --------------------------------------------------------------------------------------
# Everything above that asserts on generated Go BUILD content goes through `FakeGazelle`, whose
# output was *shaped* to match what the vendored binary was measured to emit. That is a fake
# agreeing with a measurement somebody took once, not with the binary. The two tests here run
# `tools/bin/gazelle` — `cli.GAZELLE_RUNNER` left `None`, so `_run_gazelle` really executes it —
# through the harness's own scratch/argv/capture path, and assert on the bytes that came back.
#
# There is deliberately NO `skipif` on the binary: it is vendored in-workspace and
# `tests/conftest.py` puts `tools/bin` on `PATH`, so its absence is a broken checkout and must
# fail exactly as `ast-grep`'s and `bazel`'s do. No Bazel runs here and none is needed — the
# generator is a tree walker, and `-external=static` was measured to use zero subprocesses of its
# own and zero network (`strace -e trace=socket,connect,sendto,sendmsg` over the invocation
# below: no matching syscall).

_LABEL_RE = re.compile(r'"((?:@|//)[^"]+)"')
"""Every Bazel label quoted in a generated file — `@repo//pkg:target`, `//pkg`, `//visibility:x`.

The labels are the part of a generated `BUILD.bazel` that a build resolves, so they are what
"the fake's output matches the binary's" has to be checked on: whitespace and statement ORDER
differ between the two by construction (the fake appends, the real generator rewrites the file
with its `load()` at the top) and neither difference can make a target fail to resolve."""

#: `acme-clitool-go`'s `internal/command` importing its SIBLING REPO, for the cross-repo test.
#:
#: Written into the plan's worktree rather than into `POLYGLOT_REPOS`, because the fleet-wide
#: fixture is the input to ~40 assertions about relocation, unions and root locks, and none of
#: them is about this edge. Nothing else in this suite has ever had one Go fixture import the
#: other, so the single most important claim the one-invocation design makes — that a sibling
#: import resolves to an in-repo label — had never been executed by anything.
#:
#: No `require github.com/acme/digest` is added to either `go.mod` on purpose: `-index=all`
#: resolves the import against the package the generator INDEXED at the other root, and the
#: whole question is whether it did.
SIBLING_IMPORT_ROOT_GO: str = (
    "package command\n"
    "\n"
    "import (\n"
    '\t"github.com/acme/digest"\n'
    '\t"github.com/spf13/cobra"\n'
    ")\n"
    "\n"
    "// Root is the clitool command tree.\n"
    "func Root() *cobra.Command {\n"
    '\treturn &cobra.Command{Use: "clitool", Short: string(digest.Sum(nil))}\n'
    "}\n"
)


def _captured_by_path(captured: Mapping[str, Sequence[SupportFile]]) -> dict[str, str]:
    """`_run_gazelle`'s per-repo capture flattened to `path → content`, asserting no path is
    claimed by two repos — the attribution is by `dest` prefix, so a collision would be silent."""
    out: dict[str, str] = {}
    for repo_id in sorted(captured):
        for file in captured[repo_id]:
            assert file.path not in out, (file.path, repo_id)
            out[file.path] = file.content
    return out


@pytest.mark.integration
def test_the_real_generator_emits_the_sub_package_targets_and_labels_the_fake_asserts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The vendored `gazelle`, run by the harness's own PASS 4 code, over the real Go fixtures.

    **What is new here.** Every other assertion in this file about generated Go content is an
    assertion about `FAKE_GAZELLE_OUTPUT` — a table whose contents were *chosen* to look like the
    binary's. This runs the binary: `cli.GAZELLE_RUNNER` is `None`, so `_run_gazelle` assembles
    the scratch, builds the one argv, executes `tools/bin/gazelle` and captures whatever came
    back. Nothing below supplies a shape for the generator to agree with.

    **The claims, and what each would catch.**

    1. *The exact captured path set.* A sub-package `BUILD.bazel` at depth 3 below the monorepo
       root (`go/clitool/internal/command/`) is CREATED, and `go/clitool/BUILD.bazel` is NOT
       captured — the directives file at a dest with no `.go` beside it really is left
       byte-identical, which is the thing a `<dest>/BUILD.bazel`-only capture would have read as
       "nothing happened" while publishing a Go package with no targets in it.
    2. *Real targets.* `go_library(` / `go_binary(` and a `load()` of `@io_bazel_rules_go`.
    3. *The labels are the BINARY's, compared to the fake's set-for-set, per file.* If the two
       ever disagree, the fake is the one that is wrong, and this fails naming the file.
    4. *The `# gazelle:prefix` directive survives a REAL rewrite of `go/digest/BUILD.bazel`* —
       the "which bytes win" question, answered against the program that writes the file rather
       than against a fake that was told to append.
    5. *Byte-identical over a second clean run into a second scratch.* `_run_gazelle`'s docstring
       claims the generator's output is deterministic; only the fake had ever "confirmed" it.

    **What this does NOT prove.** No Bazel has ever loaded these files — nothing here says
    `@com_github_spf13_cobra` exists, that `go_deps` creates it under that name, or that the
    labels resolve at analysis time. No Go was compiled by anything. This is the generator's
    output, captured by the harness, and nothing downstream of it.
    """
    monkeypatch.setattr(cli, "GAZELLE_RUNNER", None)
    plans = _go_build_plans(tmp_path / "worktrees")
    order = [plans["acme-clitool-go"], plans["acme-digest-go"]]

    captured = asyncio.run(cli._run_gazelle(order, binary="gazelle", scratch=tmp_path / "one"))
    files = _captured_by_path(captured)

    assert set(captured) == set(GO_DESTS), captured
    assert sorted(files) == [
        "go/clitool/cmd/clitool/BUILD.bazel",
        "go/clitool/internal/command/BUILD.bazel",
        "go/digest/BUILD.bazel",
    ], sorted(files)

    deep = "go/clitool/internal/command/BUILD.bazel"
    assert deep.count("/") >= 3, deep
    binary, internal = files["go/clitool/cmd/clitool/BUILD.bazel"], files[deep]
    digest = files["go/digest/BUILD.bazel"]

    named = (("cmd/clitool", binary), ("internal/command", internal), ("digest", digest))
    for label, body in named:
        assert "go_library(" in body, (label, body)
        assert "@io_bazel_rules_go//go:def.bzl" in body, (label, body)
    assert "go_binary(" in binary, binary
    assert 'importpath = "github.com/acme/clitool/internal/command"' in internal, internal

    for dest, entries in sorted(FAKE_GAZELLE_OUTPUT.items()):
        for relative, fake_text in sorted(entries.items()):
            path = f"{dest}/{relative}"
            assert set(_LABEL_RE.findall(files[path])) == set(_LABEL_RE.findall(fake_text)), (
                f"the real generator's labels for {path} are not the ones `FAKE_GAZELLE_OUTPUT` "
                f"asserts everywhere else in this file; the FAKE is what must change:\n"
                f"real: {sorted(set(_LABEL_RE.findall(files[path])))}\n"
                f"fake: {sorted(set(_LABEL_RE.findall(fake_text)))}"
            )

    assert '"@com_github_spf13_cobra//:go_default_library"' in internal, internal
    assert '"@org_golang_x_crypto//blake2b:go_default_library"' in digest, digest
    assert '"//go/clitool/internal/command"' in binary, binary
    assert 'visibility = ["//go/clitool:__subpackages__"]' in internal, internal
    assert "# gazelle:prefix github.com/acme/digest" in digest, (
        "the real generator rewrote `go/digest/BUILD.bazel` without keeping the directives the "
        f"harness planted, so nothing resolves an intra-repo import any more:\n{digest}"
    )

    again = _captured_by_path(
        asyncio.run(cli._run_gazelle(order, binary="gazelle", scratch=tmp_path / "two"))
    )
    assert again == files, "the generator's output is not byte-identical across two clean runs"


@pytest.mark.integration
def test_the_real_generator_resolves_a_cross_repo_import_to_an_in_repo_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Go repo importing its SIBLING repo resolves to `//go/digest` — and only because both
    roots were on one command line.

    **This is the assertion the whole one-invocation design exists for, and nothing had ever
    made it.** Both Go fixtures are self-contained: neither imports the other, so every test
    above could pass over a generator that resolved cross-repo edges to nothing at all. A
    monorepo migration whose generated packages express no edges between the repos it merged has
    produced a directory of unrelated projects.

    `internal/command` is given an `import "github.com/acme/digest"` in the plan's worktree (see
    `SIBLING_IMPORT_ROOT_GO`) and the real generator is run twice over the SAME source:

    * over **both** roots — the production argv — where the edge must appear as the in-repo label
      `//go/digest`, and must NOT appear as an external `@com_github_acme_digest`, which is what
      an out-of-tree resolution of the same import would spell;
    * over the importer's root **alone**, the negative control, where the edge is **silently
      dropped and the generator still exits 0** — no label, no external repo, no warning, no
      failure. That is the measured behaviour, it is the reason `_gazelle_files` groups by
      ecosystem instead of running per repo, and asserting only "no external label appeared"
      would be satisfied by it. Hence both directions: the edge is asserted PRESENT in the
      first and ABSENT in the second, so the one-invocation argv is what is under test rather
      than the absence of a spelling.

    **What this does NOT prove.** That `//go/digest` is a target Bazel can load: no Bazel ran,
    no Go was compiled, and `go/digest`'s own `BUILD.bazel` is asserted here only as text.
    """
    monkeypatch.setattr(cli, "GAZELLE_RUNNER", None)
    plans = _go_build_plans(tmp_path / "worktrees")
    importer = plans["acme-clitool-go"]
    source = importer.worktree / importer.dest / "internal/command" / "root.go"
    source.write_text(SIBLING_IMPORT_ROOT_GO, encoding="utf-8")
    assert "github.com/acme/digest" in source.read_text(encoding="utf-8"), source

    together = _captured_by_path(
        asyncio.run(
            cli._run_gazelle(
                [importer, plans["acme-digest-go"]], binary="gazelle", scratch=tmp_path / "both"
            )
        )
    )
    edge = together["go/clitool/internal/command/BUILD.bazel"]
    assert '"//go/digest"' in edge, (
        "the import of the sibling REPO did not resolve to an in-repo label, so the monorepo's "
        f"generated packages express no edge between the two repos it merged:\n{edge}"
    )
    assert "com_github_acme_digest" not in "\n".join(together.values()), (
        "the sibling repo resolved as an EXTERNAL module — a `go_deps`-created repo for a module "
        f"that lives in this tree and is not fetchable anywhere:\n{edge}"
    )
    assert '"@com_github_spf13_cobra//:go_default_library"' in edge, edge

    alone = _captured_by_path(
        asyncio.run(cli._run_gazelle([importer], binary="gazelle", scratch=tmp_path / "alone"))
    )
    dropped = alone["go/clitool/internal/command/BUILD.bazel"]
    assert '"@com_github_spf13_cobra//:go_default_library"' in dropped, (
        "the one-root control generated nothing usable, so its silence about the sibling proves "
        f"nothing about the roots on the command line:\n{dropped}"
    )
    assert "//go/digest" not in dropped and "acme_digest" not in dropped, (
        "the one-root invocation resolved the cross-repo edge after all; the negative control no "
        f"longer controls for anything and the test above proves less than it claims:\n{dropped}"
    )


@pytest.mark.integration
def test_a_real_fleet_build_publishes_the_real_generators_sub_packages_onto_the_branch(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    bazel: FakeBazel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`fleet build` with BOTH the real generator and the real Go resolver, asserted on what is
    ON the `integration` branch — the half of ADR-0056 consequence 8 nothing had ever run.

    **The gap this closes, stated as the two disconnected halves it replaces.** The vendored
    `gazelle` was exercised only by calling `cli._run_gazelle` **directly** (the two tests above),
    and the `fleet build` → `materialize` → `_publish` path over generator output was exercised
    only through `FakeGazelle`. So the harness had a real generator that nothing published, and a
    publish path that had only ever carried a fake's bytes. Nothing said that a real `fleet build`
    run puts the real binary's `BUILD.bazel` files — sub-packages included — on the branch. That
    is one claim and it cannot be assembled from two tests that never meet: the fake CREATES the
    same paths the binary does, by construction, so every "the sub-package is on the branch"
    assertion above is satisfied by a table.

    **Both seams are absent, and the second one is a choice with a reason.** `GAZELLE_RUNNER` is
    `None` because that is the point. `RESOLVER_RUNNER` is `None` too, because the union `go.mod`
    is the module graph `-external=static` resolves every out-of-tree import against, and PASS 4
    runs after PASS 3 precisely so that the generator reads *the resolve's* output — running the
    real generator over the fake's lock would leave that ordering claim as untested as it was.
    It is close to free: the vendored `go` resolves this closure against the workspace module
    cache under `tools/go/` in ~0.04 s and was measured to make zero network connections
    (`strace -f -e trace=connect`: not one syscall). `BAZEL_RUNNER` and `FILTER_REPO_RUNNER` stay
    fake and no Bazel runs here: the claim is about the PUBLISH path, which needs none, and
    paying for a Go-sized output base per Go repo would buy this test nothing it asserts.

    **The fleet is bounded to the two Go repos** — with the real resolver installed, a fleet
    still carrying the TypeScript and Python fixtures would resolve against the npm registry and
    PyPI on every run, which is the network dependence `FakeResolver` exists to keep out of the
    offline suite. Since ADR-0055 `--repo` would not do: it narrows dispatch and never the
    domain, so the resolve would still be the whole fleet's.

    **The claims, and what each would catch.**

    1. *One invocation, and the exact planned path set.* Recorded off `cli._run_gazelle` itself
       — the real function still runs, the wrapper only reads what it returned — so "planned"
       here is this run's plan and not a restatement of it.
    2. *A sub-package at depth 3 is ON THE BRANCH with real `go_library(` content*, read out of
       git rather than off a worktree or the plan. A worktree read would pass over a file the
       publish step never staged, which is exactly the defect `_publish`'s literal pathspec had.
    3. *Published bytes == planned bytes, per path, for every captured file* (ADR-0054's
       contract), asserted on the publish COMMIT's own tree as well as the branch tip so a later
       repo's correct publish cannot paper over an earlier one's wrong one.
    4. *`go/clitool/BUILD.bazel` is the directives-only render, byte for byte.* The real binary
       leaves it untouched — there is no `.go` file at that dest — so it is NOT captured, and the
       bytes on the branch must be the render `buildgen` wrote. That is correct rather than a
       shortfall, and pinning it is what would catch a capture that started claiming the file.
    5. *The root `go.sum` on the branch is the real resolution*, which is what says the generator
       above ran after a real PASS 3 rather than beside a canned lock.

    **What this does NOT prove.** No Bazel ran. Nothing here says `@com_github_spf13_cobra` or
    `@org_golang_x_crypto` exist under those names, that `go_deps` creates them, or that any
    label in the published files resolves at analysis time — they are text on a branch. No Go was
    compiled by anything. The history rewrite is still `FakeFilterRepo`, so this is the tree at
    the tip and not the commits behind it. And the cross-repo edge of
    `test_the_real_generator_resolves_a_cross_repo_import_to_an_in_repo_label` is NOT exercised
    here: these two fixtures import each other nowhere, so what is published is each repo's own
    packages.
    """
    _ = bazel
    monkeypatch.setattr(cli, "GAZELLE_RUNNER", None)
    monkeypatch.setattr(cli, "RESOLVER_RUNNER", None)
    assert cli.GAZELLE_RUNNER is None, "the seam must be absent for this test to mean anything"
    assert cli.RESOLVER_RUNNER is None, "the seam must be absent for this test to mean anything"

    planned: dict[str, str] = {}
    invocations: list[tuple[str, ...]] = []
    real_run_gazelle = cli._run_gazelle

    async def recording(plans: Sequence[Any], **kwargs: Any) -> dict[str, Any]:
        """`cli._run_gazelle`, run unchanged, with its RETURN VALUE read on the way past.

        The planned bytes exist only inside the run — `_BuildPlan.gazelle_files` is never
        persisted — so the alternative to reading them here is to re-run the generator in the
        test and compare the branch against *that*, which asserts two invocations agreed rather
        than that this one's plan reached the branch.
        """
        captured = await real_run_gazelle(plans, **kwargs)
        invocations.append(tuple(sorted(plan.repo_id for plan in plans)))
        for repo_id in sorted(captured):
            for support in captured[repo_id]:
                assert support.path not in planned, (support.path, repo_id)
                planned[support.path] = support.content
        return captured

    monkeypatch.setattr(cli, "_run_gazelle", recording)

    only_repos(fleet, list(GO_DESTS))
    transformed(fleet)
    assert build(fleet, "--no-sandbox").exit_code == ExitCode.SUCCESS

    # (1) One invocation over both roots, and the paths the real binary created or rewrote.
    assert invocations == [tuple(sorted(GO_DESTS))], invocations
    assert sorted(planned) == [
        "go/clitool/cmd/clitool/BUILD.bazel",
        "go/clitool/internal/command/BUILD.bazel",
        "go/digest/BUILD.bazel",
    ], sorted(planned)

    # (3) The branch tip carries exactly those bytes…
    on_branch = {path: _generated(monorepo, path) for path in sorted(planned)}
    assert on_branch == planned, "the published bytes are not the planned bytes"
    # …and so does each repo's own publish commit, which is the object that actually staged them.
    for dest in sorted(GO_DESTS.values()):
        sha = publish_commit(monorepo, dest)
        owned = sorted(path for path in planned if path.startswith(f"{dest}/"))
        assert owned, dest
        for path in owned:
            assert blob(monorepo, sha, path) == planned[path], (dest, path)

    # (2) The sub-package, at depth, with content only a Go build-file generator writes.
    deep = "go/clitool/internal/command/BUILD.bazel"
    assert deep.count("/") >= 3, deep
    body = on_branch[deep]
    assert body.startswith('load("@io_bazel_rules_go//go:def.bzl", "go_library")'), body
    assert "go_library(" in body, body
    assert 'name = "command"' in body, body
    assert 'srcs = ["root.go"]' in body, body
    assert 'importpath = "github.com/acme/clitool/internal/command"' in body, body
    assert 'visibility = ["//go/clitool:__subpackages__"]' in body, body
    assert '"@com_github_spf13_cobra//:go_default_library"' in body, body
    binary = on_branch["go/clitool/cmd/clitool/BUILD.bazel"]
    assert "go_binary(" in binary and '"//go/clitool/internal/command"' in binary, binary

    # (4) The dest-level file the generator left alone is the directives-only render, unchanged.
    ecosystems.discover()
    adapter = ecosystems.for_ecosystem(Ecosystem.GO)
    units = _go_units_from_fixture_manifests()
    directives = render_gazelle_build(adapter.gazelle_config(units["acme-clitool-go"]))
    assert "go/clitool/BUILD.bazel" not in planned, planned
    assert _generated(monorepo, "go/clitool/BUILD.bazel") == directives, (
        "the dest-level BUILD file the real generator leaves byte-identical is not the render "
        "`buildgen` wrote, so something between the render and the branch rewrote it"
    )
    assert "go_library(" not in directives, directives
    # `go/digest` is the flat layout, so ITS dest-level file really was rewritten — and the
    # directives survived the rewrite, which is what resolves an intra-repo import to a label.
    digest = on_branch["go/digest/BUILD.bazel"]
    assert digest != render_gazelle_build(adapter.gazelle_config(units["acme-digest-go"])), digest
    assert "# gazelle:prefix github.com/acme/digest" in digest, digest
    assert '"@org_golang_x_crypto//blake2b:go_default_library"' in digest, digest
    # …and it is a REWRITE, `load()` first and the harness's directives below it — which is the
    # one assertion in this test that `FakeGazelle` could not satisfy: the fake APPENDS, so its
    # `go/digest/BUILD.bazel` starts with the generated header and reaches `load(` at the end.
    # Without it, every claim above is one a sufficiently well-shaped table also passes.
    assert digest.startswith('load("@io_bazel_rules_go//go:def.bzl", "go_library")'), digest
    assert digest.index("# gazelle:prefix") > digest.index("go_library"), digest

    # (5) The generator ran after a REAL resolve, not beside a canned lock.
    assert root_file(monorepo, "go.sum") == GO_ROOT_GO_SUM, root_file(monorepo, "go.sum")
    assert root_file(monorepo, "go.mod") == GO_ROOT_GO_MOD, root_file(monorepo, "go.mod")


# ---------------------------------------------------------------------------------------
# 9. the REAL Go resolver — the root `go.sum` as a resolution, not a fixture
# ---------------------------------------------------------------------------------------
# Everything in section 6 that asserts about the root `go.sum` asserts about
# `FAKE_LOCKS["go"]`, a canned string chosen to look like a lockfile. That is enough for the
# plumbing claims — which file was staged, which command ran over it, which bytes reached the
# branch — and it is not enough for the one claim `go_deps.from_file` actually depends on: that
# the sums on the branch VERIFY the module zips Bazel will fetch. `sums_from_go_mod` reads this
# file and compares hashes; a fixture's bytes there are a checksum mismatch, which surfaces
# inside a ruleset and reads as a supply-chain compromise. So this section runs the real
# resolver, through the harness's own `cli._resolved_support_files`, with the seam absent.
#
# `tools/bin/go` is vendored in this workspace and `tests/conftest.py` puts it on `PATH`, so its
# absence is a broken checkout and must FAIL here exactly as `ast-grep`'s, `bazel`'s, `cargo`'s
# and `gazelle`'s do. There is no `skipif` on it for the same reason there is none on those.
#
# Measured, on this host, with the workspace module cache under `tools/go/` warm: the resolve
# makes ZERO network connections (`strace -f -e trace=connect` over the invocation: not one
# syscall, AF_INET or otherwise) and takes ~0.04 s. It is not in the live-network flake class.
# `GOPROXY=off` is deliberately NOT set: the harness ships one `Resolution.env`
# (`GOTOOLCHAIN`), and a test that pinned a second variable would be asserting against an
# environment production never has.

#: The fleet's root `go.sum`, verbatim, as `tools/bin/go mod download all` (Go 1.24.12) resolves
#: it from `GO_ROOT_GO_MOD` — twelve modules, each with BOTH hash kinds.
#:
#: Pinned as bytes because a module's hashes are immutable: `h1:` is the hash of a published
#: module zip and `/go.mod h1:` of its `go.mod`, both at a version this fixture fleet pins, so a
#: byte that moves here is either a different closure or a different toolchain — which is exactly
#: what `go.py:resolution` pins `GOTOOLCHAIN` to stop happening silently.
#:
#: **These bytes did NOT move when `_GO_VERSION` went 1.23.4 → 1.24.12, and that is the evidence
#: for the sentence above, not a coincidence to be glossed.** Re-resolving the new
#: `GO_ROOT_GO_MOD` (whose `go` line is the only thing that changed) under the new SDK produced a
#: byte-identical file: a `go.sum` entry is a content hash of a published module zip, and the
#: build list MVS computes from this `require` set does not depend on the toolchain's own
#: version. The `go` directive would only move the closure if it crossed the 1.17 module-graph
#: pruning boundary, which neither version is near.
#:
#: It KEEPS its trailing newline, and that is not an inconsistency with `GO_ROOT_GO_MOD` next
#: door: `SupportFile` is a `FleetModel` whose `str_strip_whitespace=True` strips content on
#: CONSTRUCTION, and a resolved lock never goes through one — `_resolved_support_files` puts the
#: resolver's bytes on the file with `model_copy`, which does not re-validate. So a synthesized
#: root file reaches disk stripped and a resolved one reaches it exactly as the tool wrote it.
GO_ROOT_GO_SUM: str = (
    "github.com/cpuguy83/go-md2man/v2 v2.0.4 h1:wfIWP927BUkWJb2NmU/kNDYIBTh/ziUX91+lVfRxZq4=\n"
    "github.com/cpuguy83/go-md2man/v2 v2.0.4/go.mod h1:"
    "tgQtvFlXSQOSOSIRvRPT7W67SCa46tRHOmNcaadrF8o=\n"
    "github.com/inconshreveable/mousetrap v1.1.0 h1:wN+x4NVGpMsO7ErUn/mUI3vEoE6Jt13X2s0bqwp9tc8=\n"
    "github.com/inconshreveable/mousetrap v1.1.0/go.mod h1:"
    "vpF70FUmC8bwa3OWnCshd2FqLfsEA9PFc4w1p2J65bw=\n"
    "github.com/russross/blackfriday/v2 v2.1.0 h1:JIOH55/0cWyOuilr9/qlrm0BSXldqnqwMsf35Ld67mk=\n"
    "github.com/russross/blackfriday/v2 v2.1.0/go.mod h1:"
    "+Rmxgy9KzJVeS9/2gXHxylqXiyQDYRxCVz55jmeOWTM=\n"
    "github.com/spf13/cobra v1.8.1 h1:e5/vxKd/rZsfSJMUX1agtjeTDf+qv1/JdBF8gg5k9ZM=\n"
    "github.com/spf13/cobra v1.8.1/go.mod h1:wHxEcudfqmLYa8iTfL+OuZPbBZkmvliBWKIezN3kD9Y=\n"
    "github.com/spf13/pflag v1.0.5 h1:iy+VFUOCP1a+8yFto/drg2CJ5u0yRoB7fZw3DKv/JXA=\n"
    "github.com/spf13/pflag v1.0.5/go.mod h1:McXfInJRrz4CZXVZOBLb0bTZqETkiAhM9Iw0y3An2Bg=\n"
    "golang.org/x/crypto v0.31.0 h1:ihbySMvVjLAeSH1IbfcRTkD/iNscyz8rGzjF/E5hV6U=\n"
    "golang.org/x/crypto v0.31.0/go.mod h1:kDsLvtWBEx7MV9tJOj9bnXsPbxwJQ6csT/x4KIN4Ssk=\n"
    "golang.org/x/net v0.21.0 h1:AQyQV4dYCvJ7vGmJyKki9+PBdyvhkSd8EIx/qb0AYv4=\n"
    "golang.org/x/net v0.21.0/go.mod h1:bIjVDfnllIU7BJ2DNgfnXvpSvtn8VRwhlsaeUTyUS44=\n"
    "golang.org/x/sys v0.28.0 h1:Fksou7UEQUWlKvIdsqzJmUmCX3cZuD2+P3XyyzwMhlA=\n"
    "golang.org/x/sys v0.28.0/go.mod h1:/VUhepiaJMQUp4+oa/7Zr1D23ma6VTLIYjOOTFZPUcA=\n"
    "golang.org/x/term v0.27.0 h1:WP60Sv1nlK1T6SupCHbXzSaN0b9wUmsPoRS9b61A23Q=\n"
    "golang.org/x/term v0.27.0/go.mod h1:iMsnZpn0cago0GOrHO2+Y7u7JPn5AylBrcoWkElMTSM=\n"
    "golang.org/x/text v0.21.0 h1:zyQAAkrwaneQ066sspRyJaG9VNi/YJ1NfzcGB3hZ/qo=\n"
    "golang.org/x/text v0.21.0/go.mod h1:4IBbMaMmOPCJ8SecivzSH54+73PCFmPWxNTLm+vZkEQ=\n"
    "gopkg.in/check.v1 v0.0.0-20161208181325-20d25e280405 h1:"
    "yhCVgyC4o1eVCa2tZl7eS0r+SDo693bJlVdllGtEeKM=\n"
    "gopkg.in/check.v1 v0.0.0-20161208181325-20d25e280405/go.mod h1:"
    "Co6ibVJAznAaIkqp8huTwlJQCZ016jof/cbN4VW5Yz0=\n"
    "gopkg.in/yaml.v3 v3.0.1 h1:fxVm/GzAzEWqLHuvctI91KS9hhNmmWOoWu0XTYJS7CA=\n"
    "gopkg.in/yaml.v3 v3.0.1/go.mod h1:K4uyk7z7BCEPqu6E+C64Yfv1cQ7kz7rIZviUmN+EgEM=\n"
)

#: One module in the resolved closure that NO fixture `go.mod` names, restated so the assertion
#: below can be read. `gopkg.in/yaml.v3` reaches the build list only through `cobra`'s own
#: `go.mod` — nothing in this repository mentions it — so its presence is the evidence that a
#: resolver walked the transitive graph rather than that a shipped file was copied.
GO_TRANSITIVE_WITNESS: str = "gopkg.in/yaml.v3"


def _go_sum_hashes(text: str) -> dict[tuple[str, str], set[str]]:
    """`go.sum` text → `{(module path, version): {hash KINDS present}}`.

    The two kinds are not interchangeable and the distinction is the whole reason `go.py` runs
    `go mod download all` rather than bare `go mod download`: `/go.mod h1:` hashes the module's
    `go.mod` (enough to compute the build list) and `h1:` hashes the module ZIP (what
    `go_deps`/`http_archive` actually verifies before extracting). A file with only the former
    is non-empty, passes the driver's "wrote no usable lock" guard, and still cannot verify a
    single download.
    """
    out: dict[tuple[str, str], set[str]] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        path, version, digest = line.split()
        assert digest.startswith("h1:"), f"not a go.sum hash line: {line!r}"
        kind = "/go.mod" if version.endswith("/go.mod") else "h1:"
        out.setdefault((path, version.removesuffix("/go.mod")), set()).add(kind)
    return out


@pytest.mark.integration
def test_the_real_go_resolver_writes_the_root_sums_for_the_whole_union_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The vendored `go`, run by the harness's own §3.3 step 2 code, over the fleet's union
    `go.mod` — and the `go.sum` that comes back is a RESOLUTION.

    **What is new here.** Every other Go lock assertion in this file is an assertion about
    `FAKE_LOCKS["go"]`. This sets `cli.RESOLVER_RUNNER` to `None`, so `_run_resolution` stages
    the scratch, merges `Resolution.env` over the real environment, executes
    `go mod download all` and reads back whatever it wrote. Nothing below supplies a shape for
    the resolver to agree with, and the sums are pinned as the bytes `go` produced.

    **The claims, and what each would catch.**

    1. *The real resolver accepts the harness's declared `Resolution` as declared.* The argv, the
       `GOTOOLCHAIN` pin and the `lock_path` are read off `go.py` and not restated, and `go`
       exiting 0 with a `go.sum` beside its input is the statement that all three are right. A
       `go mod tidy` here would delete the `require` block and write nothing; a bare
       `go mod download` would write a file with no `h1:` line in it.
    2. *Every module in the closure carries BOTH hash kinds.* `go_deps` verifies module ZIPS, so
       a `/go.mod`-only entry is a sum that cannot verify a download — the silent half-answer
       `go.py:_RESOLVER` names.
    3. *The closure is TRANSITIVE, and the union's own requirements are a strict subset of it.*
       `gopkg.in/yaml.v3` is in the resolved file and in no `go.mod` anywhere in this repository:
       it is reachable only through `cobra`'s own module graph. That is what distinguishes a
       resolve from a copy, and it is the assertion neither fixture's shipped `go.sum` — nor any
       concatenation of them — could be made to pass by accident, because it is checked against
       the coordinates the fixtures actually declare.
    4. *The union `go.mod` is byte-identical after the resolve.* `-mod=readonly` has been the
       default since Go 1.16, and the entire `lock_path`/`inputs` split rests on it: the sums are
       hashes taken against the file that LANDS, so a resolver that rewrote its own input would
       make the two disagree and every hash in the file would be individually correct.
    5. *It is neither fixture's shipped `go.sum`.* Both ship a real one, generated by this same
       binary against their OWN `go.mod`, and `cli._carried` would promote one of them the moment
       a `carry_from` reappeared on this file — skipping the resolver entirely.

    **What this does NOT prove.** No Bazel has loaded these sums: `go_deps.from_file` has never
    read this file, no module zip has been fetched by Bazel, no `BUILD.bazel` was generated here
    and not one line of Go was compiled. It proves that the bytes on the branch are a real Go
    resolution of the fleet's union — which is the precondition for asking Bazel anything at all,
    not an answer from it.
    """
    monkeypatch.setattr(cli, "RESOLVER_RUNNER", None)
    assert cli.RESOLVER_RUNNER is None, "the seam must be absent for this test to mean anything"
    ecosystems.discover()
    adapter = ecosystems.for_ecosystem(Ecosystem.GO)
    units = list(_go_units_from_fixture_manifests().values())

    #: The fixtures on disk, `go.sum` decoys included — `_carried` is asked about the real trees.
    worktrees: list[Path] = []
    for repo_id, dest in GO_DESTS.items():
        worktree = tmp_path / "worktrees" / repo_id
        for name, text in POLYGLOT_REPOS[repo_id].items():
            path = worktree / dest / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        assert (worktree / dest / "go.sum").is_file(), "the decoy this must not carry is absent"
        worktrees.append(worktree)

    plan = adapter.resolution(units)
    assert plan is not None
    assert list(plan.argv) == ["go", "mod", "download", "all"], plan.argv
    assert plan.lock_path == "go.sum", plan.lock_path
    (declared_input,) = plan.inputs
    assert declared_input.path == "go.mod", declared_input
    assert declared_input.content == GO_ROOT_GO_MOD, declared_input.content

    scratch = tmp_path / "scratch"
    resolved = asyncio.run(
        cli._resolved_support_files(
            adapter,
            units,
            repo_id="go-real-resolve",
            worktrees=worktrees,
            scratch=scratch,
        )
    )
    files = {support.path: support.content for support in resolved}
    assert set(files) == {"go.mod", "go.sum"}, files

    # (4) The resolver's own input, on disk, AFTER it ran. `-mod=readonly` is the default since
    # Go 1.16, and it is what makes "the sums hash the go.mod that lands" true by construction.
    assert (scratch / "go.mod").read_text(encoding="utf-8") == declared_input.content, (
        "`go mod download all` REWROTE the go.mod it was resolving, so the sums it wrote are "
        "hashes of a file that is not the one landing at the monorepo root"
    )
    assert files["go.mod"] == declared_input.content, files["go.mod"]

    sums = files["go.sum"]
    assert sums == GO_ROOT_GO_SUM, sums
    assert (scratch / "go.sum").read_text(encoding="utf-8") == GO_ROOT_GO_SUM, (
        "the bytes `go` wrote and the bytes the driver carried forward are not the same file"
    )

    # (2) Both hash kinds, for every module in the closure.
    present = _go_sum_hashes(sums)
    half = sorted(module for module, kinds in present.items() if kinds != {"h1:", "/go.mod"})
    assert not half, (
        "these modules have only one of the two hash kinds, so the sums cannot verify what "
        f"`go_deps` downloads — bare `go mod download` writes exactly this file: {half}"
    )

    # (3) The closure is transitive: every requirement of the union is in it, and so is at least
    # one module no `go.mod` in this repository names.
    required = {
        (f"{coordinate.group}/{coordinate.name}", coordinate.version_spec)
        for unit in units
        for coordinate in unit.external_coordinates
    }
    assert required <= set(present), sorted(required - set(present))
    transitive = sorted({path for path, _ in present} - {path for path, _ in required})
    assert GO_TRANSITIVE_WITNESS in transitive, (
        f"{GO_TRANSITIVE_WITNESS} is in no fixture's `go.mod` and reaches the build list only "
        "through cobra's own module graph; without it these sums are consistent with a shipped "
        f"file having been copied rather than a resolver having run — resolved: {transitive}"
    )

    # (5) …and it is neither repo's own file, which `cli._carried` would have promoted.
    for repo_id in GO_DESTS:
        assert sums != POLYGLOT_REPOS[repo_id]["go.sum"], (
            f"{repo_id}'s own go.sum reached the monorepo root; those hashes are taken against "
            "ITS go.mod and are missing entries for every other Go repo's modules"
        )


# ---------------------------------------------------------------------------------------
# 10. the generated Go tree under REAL Bazel
# ---------------------------------------------------------------------------------------
# Sections 8 and 9 produce the two halves of a Go monorepo — the real generator's `BUILD.bazel`
# files and the real resolver's root `go.sum` — and neither has ever been handed to a build
# system. ADR-0056 consequence 5 says so in as many words: "No Bazel has ever loaded a file this
# generator produced … Labels are asserted as text." The two tests here close that by assembling
# the tree Phase 3 publishes out of the harness's OWN bytes and asking `bazel`.
#
# **Why the tree is assembled here rather than driven through `fleet build`.** The `//go/...`
# claim is Bazel's verdict, and today the tree LOADS (the first test below) and does not yet
# BUILD: one SDK/gazelle version conflict remains, which the second test applies as text. A
# `fleet build` end-to-end would therefore report the harness's exit code for a failure that
# happens *inside* Phase 3's per-repo `bazel build`, three retries deep, after paying for one
# Go-sized output base per Go repo. Every byte below is still the harness's: the package
# `BUILD.bazel` files come back from `cli._run_gazelle` running the vendored generator, the root
# `go.mod` and `go.sum` from `cli._resolved_support_files` running the vendored `go`, and the root
# `MODULE.bazel`, `BUILD.bazel` and per-dest directives files from the three shipped renderers.
# Nothing here is hand-written for the test except the two `.bazelrc` lines and `.bazelversion`,
# which change how long a fetch waits and which Bazel runs — never what Bazel analyses.
#
# One deliberate infidelity, disclosed: `_go_build_plans` puts `FAKE_LOCKS["go"]` on the plans'
# `workspace_files`, so the `go.sum` inside the GENERATOR's scratch is the fake's. The generator
# never reads `go.sum` (it resolves out of `go.mod`), and the `go.sum` that lands in the tree
# Bazel reads is the real resolver's, which is the only copy `sums_from_go_mod` ever sees.


def _publish_generated_go_monorepo(workspace: Path, scratch: Path, *, fetch_bazelrc: str) -> None:
    """The monorepo Phase 3 would publish for the two Go fixtures, from the harness's own bytes.

    The sibling import of `SIBLING_IMPORT_ROOT_GO` is planted for section 8's reason and one
    more: `//go/clitool/internal/command` depending on `//go/digest` is the ONE edge a monorepo
    migration exists to create, and until a build system resolves it, it is a string in a file.
    """
    ecosystems.discover()
    adapter = ecosystems.for_ecosystem(Ecosystem.GO)
    plans = _go_build_plans(scratch / "worktrees")
    importer = plans["acme-clitool-go"]
    (importer.worktree / importer.dest / "internal/command" / "root.go").write_text(
        SIBLING_IMPORT_ROOT_GO, encoding="utf-8"
    )
    order = [plans["acme-clitool-go"], plans["acme-digest-go"]]

    captured = _captured_by_path(
        asyncio.run(cli._run_gazelle(order, binary="gazelle", scratch=scratch / "gazelle"))
    )
    units = _go_units_from_fixture_manifests()
    resolved = asyncio.run(
        cli._resolved_support_files(
            adapter,
            list(units.values()),
            repo_id="go-real-bazel",
            worktrees=[plan.worktree for plan in order],
            scratch=scratch / "resolve",
        )
    )

    for plan in order:
        shutil.copytree(plan.worktree / plan.dest, workspace / plan.dest, dirs_exist_ok=True)
        assert plan.gazelle is not None, plan.repo_id
        (workspace / plan.dest / cli.GENERATED_BUILD_FILE).write_text(
            render_gazelle_build(plan.gazelle), encoding="utf-8"
        )
    # …and the generator's captured output OVER it, which is the publish order `_publish` uses:
    # `go/digest/BUILD.bazel` is a rewrite of the directives file, `go/clitool/BUILD.bazel` is not
    # captured at all and must stay the directives-only render.
    for path in sorted(captured):
        target = workspace / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(captured[path], encoding="utf-8")
    assert '"//go/digest"' in captured["go/clitool/internal/command/BUILD.bazel"], (
        "the generator did not resolve the sibling import to an in-repo label, so the tree below "
        "has no cross-repo edge for Bazel to resolve and the whole point of this test is gone"
    )

    roots = {file.path: file.content for file in resolved}
    assert set(roots) == {"go.mod", "go.sum"}, roots
    for path in sorted(roots):
        (workspace / path).write_text(roots[path], encoding="utf-8")
    (workspace / cli.ROOT_PACKAGE_PATH).write_text(
        render_root_package(sorted(roots)), encoding="utf-8"
    )
    (workspace / "MODULE.bazel").write_text(
        render_module_bazel(
            [dep for unit in units.values() for dep in adapter.workspace_deps(unit)],
            module_name="acme_monorepo",
            ruleset_versions=dict(BuildSection().ruleset_versions),
            toolchains=adapter.toolchain_requirements(),
        ),
        encoding="utf-8",
    )
    (workspace / ".bazelversion").write_text(f"{MONOREPO_BAZEL_VERSION}\n", encoding="utf-8")
    (workspace / ".bazelrc").write_text(fetch_bazelrc, encoding="utf-8")


def _bazel_over_workspace(
    workspace: Path,
    startup: tuple[str, ...],
    *args: str,
    registry: tuple[str, ...],
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Real bazel in `workspace`, with the registry the session resolved appended last.

    Deliberately not `test_bazel._bazel`: this file's invocations need a 1800 s timeout and, for
    one caller, a replaced environment. The two helpers no longer DISAGREE about fetching, which
    they used to: `_bazel` rode `--http_timeout_scaling=5.0` on registry-carrying commands and
    named no repository cache at all, so it would have overridden the 8.0 this file's
    `bazel_fetch_bazelrc` writes into the workspace `.bazelrc` while re-downloading archives that
    were already cached. It now emits `conftest.HTTP_TIMEOUT_SCALING` and
    `conftest.BAZEL_REPOSITORY_CACHE` — the same two values, from the same two constants.

    `env=None` inherits this process's environment, which is what every caller but one wants.
    The exception is the no-C-compiler test below, which must change `PATH` for ONE invocation
    and for nothing else — `monkeypatch.setenv` would also change it for the session-scoped
    Bazel servers other tests are sharing.
    """
    return subprocess.run(  # noqa: S603
        [*startup, *args, *registry],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
        timeout=1800.0,
        env=None if env is None else dict(env),
    )


#: The refusals the SDK/gazelle pin conflict used to produce, named so that a regression says
#: which one came back rather than "Bazel exited 1".
#:
#: There is no repair ladder here any more, and the absence is the point: this file used to carry
#: a `GO_SDK_PIN_LADDER` whose one rung rewrote `go_sdk.download(version = "1.23.4")` to
#: `"1.24.12"` inside the generated `MODULE.bazel` before Bazel would build the tree. That edit
#: was a real defect written down as a test fixture — `go.py:_GO_VERSION` was below the floor
#: BOTH pinned rulesets impose (gazelle 0.52.2's `go.work` requires `go >= 1.24.12`; rules_go
#: 0.61.1's own `go.mod` declares `go 1.24.0`), so `go_deps` could not build the tools it
#: generates each fetched module's BUILD files with, for any Go repo with any external
#: dependency. `_GO_VERSION` is now 1.24.12 and the tests below run over the harness's UNMODIFIED
#: output.
GO_SDK_REFUSALS: tuple[str, ...] = (
    "failed to build tools: go: go.work requires go >= 1.24.12",
    "GOTOOLCHAIN=local",
    "no such package '@@gazelle++go_deps+com_github_spf13_cobra//'",
)


@pytest.mark.integration
def test_real_bazel_loads_the_generated_go_tree_because_both_names_are_io_bazel_rules_go(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bazel_workspace: Path,
    bazel_startup_argv: tuple[str, ...],
    bazel_registry_args: tuple[str, ...],
    bazel_fetch_bazelrc: str,
) -> None:
    """**The repair, asserted where the defect was.** Real Bazel turns the Go tree this harness
    generates into targets, and it can do that only because everything the harness emits now
    spells `rules_go` the one way its own generator does.

    * The vendored generator writes `load("@io_bazel_rules_go//go:def.bzl", …)` into every Go
      package it creates — asserted as text by section 8, and `@io_bazel_rules_go` is what
      bazel-gazelle has always emitted.
    * `render_module_bazel` now writes `bazel_dep(name = "rules_go", version = …, repo_name =
      "io_bazel_rules_go")`, from `GoAdapter.ruleset_repo_names`. A module is visible under its
      MODULE name unless the DEPENDENT says otherwise — the `module(repo_name = …)` in rules_go's
      own `MODULE.bazel` governs only how it sees itself — so this line is the only thing that
      can put `@io_bazel_rules_go` in scope, and upstream `bazel-gazelle` declares its own
      `bazel_dep` exactly this way.
    * `GoAdapter.extension_bzl["go_sdk"]` says `@io_bazel_rules_go//go:extensions.bzl`, because a
      `bazel_dep` has exactly ONE apparent name: while it said `@rules_go` the two labels could
      not both resolve, and the extension one failed first (`no repo visible as '@rules_go'
      here`, while Bazel was still computing the main repository mapping).

    What this replaces was a guard on the defect: with no `repo_name`, `//go/...` was not a set of
    targets that failed, it was a directory Bazel refused to walk (`No repository visible as
    '@io_bazel_rules_go' from main repository`) — for every Go repo in the fleet at once, at
    loading time, before analysis.

    **`query`, deliberately, and it is the strongest available proof of loading.** It forces
    exactly the two phases the mismatch broke and nothing after them: Bazel computes the main
    repository mapping (which is where the `go_sdk` extension label is resolved and where the old
    second failure lived) and then loads every package under `go/`, compiling each generated
    `BUILD.bazel` — which means fetching `rules_go` and evaluating `load("@io_bazel_rules_go…")`
    for real. It stops short of fetching the `go_deps` module repos, which is where the SDK /
    gazelle version conflict used to live; the next test is the one that goes there.

    It does NOT prove the tree BUILDS — the next test does, over the same unmodified bytes — and
    it proves nothing about `fleet build` end to end.
    """
    monkeypatch.setattr(cli, "GAZELLE_RUNNER", None)
    monkeypatch.setattr(cli, "RESOLVER_RUNNER", None)
    _publish_generated_go_monorepo(bazel_workspace, tmp_path, fetch_bazelrc=bazel_fetch_bazelrc)

    module = (bazel_workspace / "MODULE.bazel").read_text(encoding="utf-8")
    assert 'bazel_dep(name = "rules_go", version = ' in module, module
    assert 'repo_name = "io_bazel_rules_go")' in module, module
    assert 'use_extension("@io_bazel_rules_go//go:extensions.bzl", "go_sdk")' in module, module
    assert "@rules_go//" not in module, module
    loaded = (bazel_workspace / "go" / "digest" / "BUILD.bazel").read_text(encoding="utf-8")
    assert "@io_bazel_rules_go//go:def.bzl" in loaded, loaded

    queried = _bazel_over_workspace(
        bazel_workspace, bazel_startup_argv, "query", "//go/...", registry=bazel_registry_args
    )
    _fail_if_registry_unreachable(queried, bazel_registry_args)
    assert queried.returncode == 0, (
        "the generated Go tree still does not load under real Bazel:\n"
        f"{queried.stderr[-4000:]}"
    )
    targets = {line.strip() for line in queried.stdout.splitlines() if line.strip()}
    assert {"//go/digest:digest", "//go/clitool/internal/command:command"} <= targets, targets
    # The refusals this repair removed — both halves of the naming mismatch and the loading-phase
    # symptom they produced — named so that a regression says which one came back.
    for refusal in (
        "No repository visible as '@io_bazel_rules_go' from main repository.",
        "no repo visible as '@rules_go' here",
        "error loading package under directory 'go'",
    ):
        assert refusal not in queried.stderr, queried.stderr[-4000:]


@pytest.mark.integration
def test_the_generated_go_tree_analyses_and_compiles_from_unmodified_harness_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bazel_workspace: Path,
    bazel_startup_argv: tuple[str, ...],
    bazel_registry_args: tuple[str, ...],
    bazel_fetch_bazelrc: str,
    bazel_output_user_root: Path,
) -> None:
    """Everything else the harness generated is right, and Bazel says so: `go_deps.from_file`
    reads the union `go.mod` and the resolved `go.sum`, the labels resolve, `//go/digest` is a
    real target `//go/clitool/internal/command` depends on, and `rustc`'s Go equivalent compiles
    every package.

    **NOTHING is edited between the harness and Bazel, and that is the claim.** This test used to
    open by rewriting `go_sdk.download(version = …)` in the generated `MODULE.bazel` — one rung of
    a `GO_SDK_PIN_LADDER` — because `go.py:_GO_VERSION` was 1.23.4 while
    `build.ruleset_versions` pinned gazelle 0.52.2, whose `go.work` requires `go >= 1.24.12`, and
    rules_go 0.61.1, whose own `go.mod` declares `go 1.24.0`. `go_deps` builds its BUILD-file
    generation tools with the registered SDK, so that pair refused *any* Go repo with *any*
    external dependency. `_GO_VERSION` is now 1.24.12 — the minimum both floors accept — and the
    ladder is gone because it is empty, not because anything below was weakened. The bytes are
    hashed before and after the build so that "unmodified" is checked rather than asserted in
    prose, and the version in the file is read off `GoAdapter.toolchain_requirements()` rather
    than restated, so a `_GO_VERSION` that moved without this tree moving with it fails here.

    The apparent-name rungs that used to precede it are gone for a different reason, recorded so
    the two are not conflated: `GoAdapter.ruleset_repo_names` gives `render_module_bazel` the
    `repo_name = "io_bazel_rules_go"` the generated `load()`s need, and `extension_bzl["go_sdk"]`
    spells that same module the same way. The test above is what proves it against real Bazel.

    **What Bazel then proves, and what it does not.**

    * `go_deps.from_file` LOADS: it read `//:go.mod` — the union of both repos' requirements —
      and the `go.sum` beside it, which is the real resolver's output. A sum that CONTRADICTS
      another module's for the same version is `fail()` inside the extension (`_safe_insert_sum`),
      and a wrong `h1:` fails the fetch, so reaching a green build is `sums_from_go_mod`'s verdict
      on section 9's bytes — for the versions Bazel actually selected, which is the caveat below.
    * The cross-repo edge is REAL, read out of `cquery` rather than out of the file: Bazel's own
      configured-target graph has `//go/digest:digest` one hop from
      `//go/clitool/internal/command:command`. That is ADR-0056 consequence 5's "asserted as
      text" answered by the build system.
    * Go is COMPILED, not merely analysed: `.a` archives are `GoCompilePkg`'s output, and the
      binary's `clitool.a` cannot be produced unless the compiler resolved
      `import "github.com/acme/digest"` through the label above.

    It does NOT prove that `fleet build` produces this tree end to end: what is published here is
    `_publish_generated_go_monorepo`'s tree, and Phase 3's own per-repo `bazel build` is not the
    invocation under test. Nor does it say anything about `_publish`, which section 6 covers over
    fake generator bytes.

    **It also does not prove that every module zip Bazel fetched is the version section 9's
    `go.sum` hashes, and that gap is real rather than rhetorical.** Verbatim from this build:

        DEBUG: …/gazelle+/internal/bzlmod/go_deps.bzl:753:36: The following Go modules were
        required by the root module at the given versions, but were implicitly updated to higher
        versions due to transitive dependencies:
          golang.org/x/crypto: v0.31.0 -> v0.39.0
          golang.org/x/sys: v0.28.0 -> v0.33.0

    `go_deps` is ONE module extension for the whole Bazel module graph, and rules_go and gazelle
    each call `go_deps.from_file` on their own `go.mod`. It therefore runs Go MVS over the union
    of all three, and one Go module path can have exactly one repository in a build — so a
    transitive floor above a harness pin RAISES it. That much is inherent to Bzlmod: there is no
    "resolve only from my `go.mod`" mode, and the per-module levers (`go_deps.module_override`,
    `archive_override`) do not change it in general.

    What is NOT inherent is the silence. `go_deps.config(check_direct_dependencies = "error")` —
    a root-module-only tag — turns that DEBUG into `fail()`, and the harness renders the root
    `MODULE.bazel`, so it could emit it. It does not today. Worse, `go_deps.bzl`'s
    `_get_sum_from_module` answers a raised version with no sum by PRINTING (`No sum for …@… found,
    run … mod tidy`) and returning `None`, which fetches that module with no checksum at all. Here
    the raised versions were covered by gazelle's own `go.sum` — no `No sum for …` line appeared
    anywhere in the build output — so the fetch WAS verified, by a third party's sums rather than
    by the ones this harness resolved. A green build means the harness's sums are CONSISTENT with
    what Bazel selected, not that they are what Bazel verified.

    **The output base is reaped at the end**, `test_two_rust_repos_in_one_wave_both_build`'s
    precedent and for its reason: a Go SDK plus `rules_go`, `gazelle` and twelve `go_deps` module
    repos is ~800 MB that only these two tests need, sitting in a session-shared root that is
    measured after every later test. The archives stay in `conftest`'s content-addressed
    `repos/`, so the cost of re-fetching is analysis time and no network.
    """
    monkeypatch.setattr(cli, "GAZELLE_RUNNER", None)
    monkeypatch.setattr(cli, "RESOLVER_RUNNER", None)
    _publish_generated_go_monorepo(bazel_workspace, tmp_path, fetch_bazelrc=bazel_fetch_bazelrc)

    # The two files the conflict lived in, as the harness wrote them — and the SDK version read
    # off the adapter rather than spelled again, so this cannot pass against a stale literal.
    (toolchain,) = ecosystems.for_ecosystem(Ecosystem.GO).toolchain_requirements()
    module_bazel = bazel_workspace / "MODULE.bazel"
    before = module_bazel.read_bytes()
    assert f'version = "{toolchain.version}",' in before.decode("utf-8"), before.decode("utf-8")
    assert (bazel_workspace / "go.mod").read_text(encoding="utf-8") == GO_ROOT_GO_MOD
    assert (bazel_workspace / "go.sum").read_text(encoding="utf-8") == GO_ROOT_GO_SUM

    built = _bazel_over_workspace(
        bazel_workspace, bazel_startup_argv, "build", "//go/...", registry=bazel_registry_args
    )
    _fail_if_registry_unreachable(built, bazel_registry_args)
    assert built.returncode == 0, built.stderr[-8000:]
    assert "Build completed successfully" in built.stderr, built.stderr[-8000:]
    # The refusals the two jointly-impossible pins produced, named individually so that a
    # regression in either says which floor was crossed rather than "Bazel exited 1".
    for refusal in GO_SDK_REFUSALS:
        assert refusal not in built.stderr, built.stderr[-8000:]
    assert module_bazel.read_bytes() == before, (
        "the generated MODULE.bazel was edited between the harness and Bazel; the point of this "
        "test is that it is not"
    )

    # The edge, from Bazel's configured-target graph and not from the generated text.
    edge = _bazel_over_workspace(
        bazel_workspace,
        bazel_startup_argv,
        "cquery",
        "deps(//go/clitool/internal/command:command, 1)",
        registry=bazel_registry_args,
    )
    assert edge.returncode == 0, edge.stderr[-4000:]
    resolved_labels = {line.split()[0] for line in edge.stdout.splitlines() if line.strip()}
    assert "//go/digest:digest" in resolved_labels, (
        "the cross-repo label is in the generated file and is not in Bazel's dependency graph"
        f":\n{edge.stdout}"
    )
    assert "@com_github_spf13_cobra//:go_default_library" in resolved_labels, edge.stdout

    # Compiled, not analysed: `GoCompilePkg` really ran for both repos' packages, and the binary's
    # own archive cannot exist unless the sibling import type-checked through `//go/digest`.
    for archive in (
        "bazel-bin/go/digest/digest.a",
        "bazel-bin/go/clitool/internal/command/command.a",
        "bazel-bin/go/clitool/cmd/clitool/clitool_lib.a",
    ):
        assert (bazel_workspace / archive).is_file(), f"{archive} was never compiled"

    reap_bazel_state(bazel_output_user_root)
    bazel_output_user_root.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------------------
# 10b. the C toolchain the Go tree above never mentions
# ---------------------------------------------------------------------------------------
#: Basenames Bazel's C++ autoconfiguration will accept as the host compiler, matched against every
#: entry on `PATH`. `cc_configure` resolves them with `repository_ctx.which()` — a PATH lookup —
#: so a PATH with none of these names on it is exactly the state a minimal container image is in,
#: while nothing else about the environment changes.
_C_COMPILER_BASENAME = re.compile(r"(?:^|-)(?:cc|gcc|g\+\+|c\+\+|cpp|clang|clang\+\+)(?:-[\d.]+)?$")

#: The verbatim mechanism, in the order Bazel prints it. Named individually so a regression says
#: WHICH link in the chain moved rather than "bazel exited 1": the autoconfiguration probe failed,
#: the repository it would have generated therefore does not exist, and the pure-Go target's
#: analysis wanted a C++ toolchain out of it anyway. The canonical repo name is Bzlmod's
#: `@@<module>+<extension>+<repo>` spelling and is asserted in full, because "something about cc
#: failed" is precisely the diagnosis this test exists to replace.
NO_C_COMPILER_REFUSALS: tuple[str, ...] = (
    "Auto-Configuration Error: Cannot find gcc or CC; either correct your path or set the CC "
    "environment variable",
    "no such package '@@rules_cc++cc_configure_extension+local_config_cc//'",
    # The two edges that make this a GO problem and not a C problem, quoted from the build below:
    # the Go standard library itself wants a C++ toolchain, and so therefore does a pure-Go
    # `go_library` that imports nothing but the standard library.
    "@@rules_go+//:stdlib depends on "
    "@@rules_cc++cc_configure_extension+local_config_cc//:cc-compiler-k8",
    "//go/digest:digest depends on "
    "@@rules_cc++cc_configure_extension+local_config_cc//:cc-compiler-k8",
)


def _path_without_a_c_compiler(shim: Path) -> str:
    """A `PATH` identical to this session's except that no C compiler resolves through it.

    Every executable currently on `PATH` is symlinked into one directory in PATH order (first
    wins, exactly as a real PATH search resolves), minus the compiler basenames above. Truncating
    `PATH` instead would also remove `bazel` (`tools/bin`), `git`, and the vendored `go` that the
    `go_deps` extension shells out to during the fetch phase — and a test that removed those
    would prove Bazel needs *a* PATH, not that it needs a C compiler.
    """
    shim.mkdir(parents=True, exist_ok=True)
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        try:
            entries = sorted(Path(directory).iterdir())
        except OSError:
            continue
        for entry in entries:
            if _C_COMPILER_BASENAME.search(entry.name):
                continue
            try:
                (shim / entry.name).symlink_to(entry)
            except OSError:
                continue  # first PATH entry wins, as a PATH search would
    for absent in ("cc", "gcc", "g++", "clang", "cpp"):
        assert shutil.which(absent, path=str(shim)) is None, f"{absent} survived the shim PATH"
    assert shutil.which("bazel", path=str(shim)) is not None, "the shim PATH lost bazel itself"
    return str(shim)


@pytest.mark.integration
def test_the_pure_go_tree_fails_to_analyse_when_no_c_compiler_is_discoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bazel_workspace: Path,
    bazel_registry_args: tuple[str, ...],
    bazel_fetch_bazelrc: str,
) -> None:
    """**Every Go target this harness publishes needs a host C compiler, cgo or not**, and the
    word `cgo` appears nowhere in `src/` — so this is the test that makes the dependency
    load-bearing instead of accidental.

    The tree is the same unmodified harness output the test above compiles green, and the target
    is `//go/digest:digest`, which is **pure Go**: neither Go fixture contains `import "C"`, so
    Gazelle emitted no `cgo = True` anywhere in this tree. It still does not analyse, because
    `rules_go`'s own `//:stdlib` depends on
    `@@rules_cc++cc_configure_extension+local_config_cc//:cc-compiler-k8`, and that repository is
    generated by resolving `gcc` on `PATH` (or `$CC`; `cc` is never searched — see
    `C_TOOLCHAIN_PROBE`). No compiler, no repository, no analysis.

    **What the assertions pin, and why each is not the obvious weaker one.**

    * The failure is at **analysis**, not at execution: `--nobuild`, `cquery` and `query --output=
      build` fail identically, so there is no "just check it loads" that routes around this.
    * `--keep_going` is asserted — and it is the flag `BuildverifyInput.keep_going` DEFAULTS to —
      because it is what makes the blast radius total rather than partial: Bazel reports
      `INFO: Found 0 targets`, so a real Phase 3 verification of a Go repo on a compiler-less
      image produces exit 1 with nothing analysed, which `classify_build_failure` reads as a
      retryable `BUILD_ERROR` and spends all three ADR-0014 rungs on. That is the defect
      `BuildverifyWorker._c_toolchain_gate` exists to convert into one non-retryable refusal.
    * The verbatim `local_config_cc` / `cc-compiler-k8` strings are asserted rather than a bare
      non-zero exit, because a Go build fails for a hundred reasons and this test is only about
      one of them.

    **The host toolchain is never touched.** `PATH` is rewritten for this ONE subprocess (see
    `_path_without_a_c_compiler`); `gcc` stays exactly where it is at `/usr/bin/gcc`.

    **Its own output base, reaped in a `finally`** — `test_two_rust_repos_in_one_wave_both_build`'s
    precedent, for a sharper reason than disk: a `local_config_cc` fetched under a broken PATH
    must not be visible to any other test, and reaping is the only bound that does not depend on
    Bazel's marker-file invalidation being right. The ruleset archives come from the session's
    content-addressed `repos/` cache, so the extra cost is analysis time, not network.

    **What this does NOT prove.** Nothing whatsoever about `settings.verify.container_image`:
    this test runs unsandboxed against the host, with no daemon and no image involved. It proves
    the DEPENDENCY exists, not that any particular image satisfies it. That the image built by
    `docker/fleet-build.Dockerfile` does satisfy it is a separate assertion, made by
    `test_sandbox.test_the_fleet_build_image_satisfies_the_c_toolchain_probe`, and even that one
    proves only the compiler lookup — no Bazel has ever run inside that image.
    """
    monkeypatch.setattr(cli, "GAZELLE_RUNNER", None)
    monkeypatch.setattr(cli, "RESOLVER_RUNNER", None)
    _publish_generated_go_monorepo(bazel_workspace, tmp_path, fetch_bazelrc=bazel_fetch_bazelrc)
    assert "cgo" not in (bazel_workspace / "go" / "digest" / "BUILD.bazel").read_text(
        encoding="utf-8"
    ), "the target this test calls pure Go has cgo in it; the claim below is no longer about cgo"

    env = dict(os.environ)
    env["PATH"] = _path_without_a_c_compiler(tmp_path / "path-without-a-c-compiler")
    for named_compiler in ("CC", "CXX", "BAZEL_COMPILER"):
        env.pop(named_compiler, None)

    output_user_root = NO_C_COMPILER_OUTPUT_USER_ROOT
    output_user_root.mkdir(parents=True, exist_ok=True)
    try:
        built = _bazel_over_workspace(
            bazel_workspace,
            ("bazel", f"--output_user_root={output_user_root}", "--noblock_for_lock"),
            "build",
            "--keep_going",
            "//go/digest:digest",
            registry=bazel_registry_args,
            env=env,
        )
        _fail_if_registry_unreachable(built, bazel_registry_args)
        reported = f"{built.stdout}\n{built.stderr}"
        assert built.returncode != 0, (
            "a Go target analysed with no C compiler on PATH; if this host grew a second "
            f"compiler the shim does not filter, the shim is what is wrong:\n{reported[-8000:]}"
        )
        for refusal in NO_C_COMPILER_REFUSALS:
            assert refusal in reported, f"{refusal!r} is not in:\n{reported[-8000:]}"
        assert "INFO: Found 0 targets" in reported, (
            "`--keep_going` left something analysable, so the blast radius is smaller than the "
            f"precondition assumes:\n{reported[-8000:]}"
        )
    finally:
        assert reap_bazel_state(output_user_root) == 0, output_user_root


#: The one `h1:` line in the resolved root `go.sum` that **no other `go.sum` in the Bazel module
#: graph supplies**, so deleting it really does leave the module uncovered rather than merely
#: shifting which contributor answers for it. Verified by grepping every `go.sum` in the session's
#: Bazel repository cache for `spf13/cobra v1.8.1 h1:` — zero hits outside this harness's own
#: resolved file, while `golang.org/x/crypto v0.39.0 h1:` and `golang.org/x/sys v0.33.0 h1:` are
#: both present in **rules_go 0.61.1's** `go.sum` (and in neither gazelle 0.52.2's nor ours).
GO_UNCOVERED_SUM_LINE: str = "github.com/spf13/cobra v1.8.1 h1:"


@pytest.mark.integration
def test_a_go_module_with_no_sum_anywhere_in_the_graph_is_refused_rather_than_fetched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bazel_workspace: Path,
    bazel_startup_argv: tuple[str, ...],
    bazel_registry_args: tuple[str, ...],
    bazel_fetch_bazelrc: str,
    bazel_output_user_root: Path,
) -> None:
    """**D19, measured instead of reasoned about — and half of it turns out to be false.**

    D19 records two things the harness could in principle fix on top of one that is inherent. The
    inherent third is real and this test pins it. The half recorded as *"a raised version with no
    sum is fetched with no checksum at all"* is **not true of the pinned gazelle**, and this test
    is what says so mechanically, because the whole claim rests on a version pin that can move.

    **What the fixture poses.** The published tree is the harness's own unmodified output, with
    exactly one line removed from the resolved root `go.sum`: `spf13/cobra`'s module-zip hash,
    chosen because no other `go.sum` in the module graph carries it (see
    `GO_UNCOVERED_SUM_LINE`). That is precisely D19's condition — a module Bazel must fetch, for
    which the union of every contributing `go.sum` has no `h1:`.

    **What Bazel does, verbatim from the run this test asserts on.** Two things happen, in order,
    and only the first is what D19 describes:

        DEBUG: …/gazelle+/internal/bzlmod/go_deps.bzl:925:18: No sum for
        github.com/spf13/cobra@1.8.1 found, run bazel run @io_bazel_rules_go//go -- mod tidy to
        generate it

        ERROR: …/gazelle+/internal/go_repository.bzl:204:21: An error occurred during the fetch of
        repository 'gazelle++go_deps+com_github_spf13_cobra':
          Error in fail: No sum for github.com/spf13/cobra@v1.8.1 found, update go.sum with:
          bazel run @io_bazel_rules_go//go -- mod tidy

    `_get_sum_from_module` does print and return `None`, exactly as D19 says. What D19 stops short
    of is where that `None` lands: `go_repository`'s implementation treats `sum` as **mandatory**
    in module mode for every repo the extension creates (`is_module_extension_repo`, set from
    `internal_only_do_not_use_apparent_name`, which `go_deps.bzl` passes for all of them) and
    `fail()`s at **fetch** time, with `GOSUMDB` forced `off` beside it for the same stated reason.
    **So nothing is ever fetched unchecked; the build refuses to proceed.** The hole is closed —
    by gazelle, not by this harness, and at a version this harness pins.

    **That is the entire reason this test is a real Bazel run and not a comment.** The safety
    property belongs to `build.ruleset_versions["gazelle"] == "0.52.2"`. A bump that restored the
    older fetch-anyway behaviour would reopen a genuine supply-chain hole with no diff anywhere in
    this repository, which is D8's shape and the shape this whole file exists to catch. This test
    turns that bump red.

    **The residual risk, stated plainly, because it is NOT closed and a reader will have to live
    with it.** The build below is deliberately allowed to be an aborted one; the *green* build in
    the test above is where the residual lives. In that build the harness's `go.sum` says
    `golang.org/x/crypto v0.31.0` and Bazel fetched **v0.39.0**, verified against **rules_go's**
    `go.sum` rather than this harness's — asserted here as a pair, since the DEBUG raise and the
    root `go.sum` line appear together in this very run. In one sentence a non-expert can act on:
    **the monorepo's `go.sum` is not a statement about which versions the monorepo builds
    against — it is one of several checksum files Bazel consults, and for any dependency that a
    pinned ruleset requires more recently than your repos do, the version and the hash that
    actually govern the build come from that ruleset's lock file, which moves when you move
    `build.ruleset_versions`.** What is guaranteed is only that *some* contributor's hash matched:
    an attacker cannot substitute bytes, but an operator cannot read `go.sum` and know what
    shipped. Fixing that would require the harness to reproduce `go_deps`' MVS over the whole
    Bazel module graph before Bazel runs — which means enumerating every module that calls
    `go_deps.from_file` and mapping each BCR version to a Go module version. Neither is knowable
    from anything the harness holds, and guessing either is how D19 got here.
    """
    monkeypatch.setattr(cli, "GAZELLE_RUNNER", None)
    monkeypatch.setattr(cli, "RESOLVER_RUNNER", None)
    _publish_generated_go_monorepo(bazel_workspace, tmp_path, fetch_bazelrc=bazel_fetch_bazelrc)

    sums_path = bazel_workspace / "go.sum"
    sums = sums_path.read_text(encoding="utf-8")
    assert sums == GO_ROOT_GO_SUM, sums
    # The residual, half of it: what the harness's own lock claims about the module the DEBUG
    # line below reports Bazel actually selecting. Both halves are read out of this one run.
    assert "golang.org/x/crypto v0.31.0 h1:" in sums, sums
    kept = [line for line in sums.splitlines() if not line.startswith(GO_UNCOVERED_SUM_LINE)]
    assert len(kept) == len(sums.splitlines()) - 1, (
        f"{GO_UNCOVERED_SUM_LINE!r} is no longer in the resolved root go.sum, so this test is "
        "removing nothing and proving nothing"
    )
    sums_path.write_text("\n".join(kept) + "\n", encoding="utf-8")

    built = _bazel_over_workspace(
        bazel_workspace, bazel_startup_argv, "build", "//go/...", registry=bazel_registry_args
    )
    _fail_if_registry_unreachable(built, bazel_registry_args)

    # 1. It FAILED, and it failed in the fetch of the uncovered module rather than anywhere else.
    assert built.returncode != 0, (
        "Bazel built the tree with no module-zip hash for github.com/spf13/cobra anywhere in the "
        "module graph, so a raised or unpinned Go module can now be fetched unverified — this is "
        "D19's second half becoming REACHABLE, and it is a supply-chain hole, not a test failure"
    )
    assert "Build completed successfully" not in built.stderr, built.stderr[-4000:]
    assert "No sum for github.com/spf13/cobra@v1.8.1 found" in built.stderr, built.stderr[-4000:]
    # …from `go_repository`'s mandatory-`sum` gate, not merely from the extension's `print`. The
    # distinction IS the finding: the extension only warns, and the refusal comes one layer down.
    assert "internal/go_repository.bzl" in built.stderr, built.stderr[-4000:]
    assert (
        "An error occurred during the fetch of repository "
        "'gazelle++go_deps+com_github_spf13_cobra'" in built.stderr
    ), built.stderr[-4000:]

    # 2. The MVS raise is still reported at DEBUG and is still not fatal — i.e. the harness emits
    #    no `go_deps.config(check_direct_dependencies = "error")`, and the modules named are the
    #    ones D19 records. An `Error in fail:` carrying this text instead would mean the posture
    #    changed; `test_the_root_module_states_no_go_deps_posture_so_gazelles_default_governs_d19`
    #    pins the same decision at the rendering layer, without Bazel.
    raise_report = (
        "The following Go modules were required by the root module at the given versions, but "
        "were implicitly updated to higher versions due to transitive dependencies"
    )
    assert raise_report in built.stderr, built.stderr[-8000:]
    reporting_line = next(line for line in built.stderr.splitlines() if raise_report in line)
    assert reporting_line.startswith("DEBUG: "), reporting_line
    assert "golang.org/x/crypto: v0.31.0 -> v0.39.0" in built.stderr, built.stderr[-8000:]
    assert "golang.org/x/sys: v0.28.0 -> v0.33.0" in built.stderr, built.stderr[-8000:]
    assert f"Error in fail: {raise_report}" not in built.stderr, (
        "the MVS raise is now a hard failure, so every fleet whose Go repos pin below a pinned "
        "ruleset's floor fails its whole monorepo build on a condition no repo caused and the "
        "harness cannot repair — see the D19 tests' docstrings before accepting this"
    )

    reap_bazel_state(bazel_output_user_root)
    bazel_output_user_root.mkdir(parents=True, exist_ok=True)




# ---------------------------------------------------------------------------------------
# 8. §12.11 Task B — real Bazel and real sandboxed Docker, TOGETHER (round VI task 57)
# ---------------------------------------------------------------------------------------
# `research-24-report.md` (round V criteria closure) sized this: real-lock-publish and
# real-sandbox proof each existed separately, but never in the SAME run. Phase A below warms
# `verify.repository_cache` and lets `cli._publish_module_lock` capture a REAL `MODULE.bazel.lock`
# for the first time (every existing test of that mechanism drives `LockWritingBazel`, a
# `FakeBazel` subclass) — AND it runs a real `bazel test` unsandboxed too, over `acme-widgets-py`
# (D112's own fixture): the first live measurement in this file found that a `bazel build`-only
# Phase A leaves the hermetic Python interpreter's RUNTIME (as opposed to its build-time stub)
# never fetched into `verify.repository_cache`, which a `--network=none` Phase B cannot then
# retrieve — `bazel test` under Docker failed every real target with `Exit 127` ("interpreter not
# found") even though the SAME build step succeeded, until Phase A itself ran a real `bazel test`
# once, unsandboxed, so the interpreter runtime the sandboxed runs need is already warm in the
# cache before `--network=none` ever applies. This is reported in full in this task's report.
#
# Phase B reuses that warm cache and that published lock for TWO real `docker run --network=none`
# builds of further, same-ecosystem (Python) repos, and asserts against the container's own
# `attempts.exit_code` and its recorded `--network=none` argv — not merely the harness's summary
# status (the ADR-0053/D13-style lesson this file repeats elsewhere).

#: Two repos deliberately NOT added to the shared `POLYGLOT_REPOS` dict above: they exist only
#: for this task's own fixture, and touching the shared dict for task-scoped repos would risk
#: every other test that keys off it. Same shape as `acme-widgets-py`'s `test_widgets.py` (D112):
#: a bare top-level `assert`, not a `def test_…():` `pytest` would collect but `py_test`'s
#: `main=` (no test-framework `deps`) never calls — the real `bazel test` executes each file as a
#: plain script, so a vacuous "pass" is not available here either.
_TASK57_HAPPY_REPO: Final[dict[str, str]] = {
    "pyproject.toml": (
        '[project]\nname = "task57-happy-py"\nversion = "0.1.0"\ndependencies = []\n'
    ),
    "task57_happy_py/__init__.py": "def double(value: int) -> int:\n    return value * 2\n",
    "task57_happy_py/test_happy.py": (
        "def _double(value: int) -> int:\n"
        "    return value * 2\n"
        "\n"
        "\n"
        "assert _double(9) == 18\n"
    ),
}

_TASK57_SHRINK_REPO: Final[dict[str, str]] = {
    "pyproject.toml": (
        '[project]\nname = "task57-shrink-py"\nversion = "0.1.0"\ndependencies = []\n'
    ),
    "task57_shrink_py/__init__.py": "def triple(value: int) -> int:\n    return value * 3\n",
    "task57_shrink_py/test_shrink.py": (
        "def _triple(value: int) -> int:\n"
        "    return value * 3\n"
        "\n"
        "\n"
        "assert _triple(7) == 21\n"
    ),
}


def _add_local_repo(root: Path, name: str, files: dict[str, str]) -> None:
    """`add_repos`'s own shape, for a repo dict that is NOT `POLYGLOT_REPOS` — called before
    `fleet scan`, same as `add_repos`."""
    sources = root.parent / "sources"
    url = _make_repo(sources, name, files)
    manifest = root / "config" / "repos.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + f"  - name: {name}\n    url: {url}\n",
        encoding="utf-8",
    )


@pytest.mark.integration
@pytest.mark.skipif(
    shutil.which("bazel") is None,
    reason="bazel is not installed on this host; §12.11 Task B needs a real bazel end to end",
)
@pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="docker is not installed on this host; §12.11 Task B needs a real sandboxed run",
)
def test_a_real_bazel_lock_publish_and_a_real_sandboxed_build_happen_in_the_same_run(
    fleet: Path,  # noqa: F811
    monorepo: Path,
    tmp_path: Path,
    bazel_cache_home: Path,
    bazel_registry: str,
    bazel_fetch_bazelrc: str,
) -> None:
    """§12.11 Task B (`docs/CRITERIA_PLAN.md` §11, `research-24-report.md`): the sandboxed path
    proven end to end, reusing a lock a real Bazel actually wrote.

    **Phase A — unsandboxed, over `acme-widgets-py` (D112's own fixture, zero external deps,
    PyPI, a real `test_widgets.py`).** `real_build`'s own seam assertions
    (`cli.BAZEL_RUNNER`/`FILTER_REPO_RUNNER`/`RESOLVER_RUNNER` all `None`) mean this really is
    `tools/bin/bazel`, and its Phase 3 publish step really does write `MODULE.bazel.lock` onto
    `integration` for the first time this mechanism has ever run against a real Bazel result.
    `verify.repository_cache` is warmed the same way `real_build` always warms it. Building AND
    testing `acme-widgets-py` here (rather than a test-less repo) is load-bearing, not
    incidental — see the module docstring above.

    **Phase B — sandboxed (the default), over TWO further same-ecosystem Python repos**
    (`task57-happy-py`, `task57-shrink-py`), each cut from `integration`'s tip AFTER Phase A's
    commit — so each build worktree already carries Phase A's lock before Bazel ever runs, and
    `verify.repository_cache` is the SAME now-warm directory (`bazel_cache_home`'s
    `XDG_CACHE_HOME` never changes mid-test; the repository cache path is a `fleet.yaml` setting
    Phase A wrote once).

    **D116, disclosed.** `docs/INTEGRATION_HONESTY.md`'s `## D116` entry (round VI task 54):
    nothing in `src/fleet/` ever writes `repos.baseline_ok`, so the shipped preflight pipeline
    can never itself produce `baseline_ok = 1` — this task's own brief anticipated exactly that
    and sanctioned seeding it directly for this fixture, which is what the `UPDATE repos`
    statements below do; the brief also said to disclose it rather than build a silent
    workaround, so it is disclosed here and in the round's report.

    **The Rule-12 discriminator, over `task57-shrink-py`.** Seeded with `baseline_test_count =
    99`, far above its real (and correct) test count of 1. The OLD boolean check
    (`no_test_targets`/`tests_lost`) stays green here — a real test target ran, under real
    Docker, and passed, so `bazel test`'s own exit code is 0 — while the NEW
    `test_count_regressed` check (`migrated_test_count < baseline_test_count`, over a REAL `bazel
    query 'tests(//<dest>/...)'` count, not `FakeBazel`'s canned table) is what actually catches
    the shrink. This is Task A's own precedent (`tests/test_workers_build.py::
    test_real_bazel_catches_a_test_count_shrink_the_boolean_check_cannot_see`), reproduced here
    reached through the full two-phase, sandboxed `fleet build` CLI path rather than at the
    worker layer directly — proving the wiring discriminates in THIS fixture, not only that the
    underlying property once did in a different one.

    **The query runs HOST-ONLY, deliberately, and that is SPEC-compliant, not an open question.**
    `BuildverifyWorker._test_query_argv` (`src/fleet/workers/buildverify.py`) never wraps `bazel
    query 'tests(//<dest>/...)'` in `docker_run_argv` — by its own docstring, "Host-only,
    deliberately." SPEC §12.11 (`docs/SPEC.md:7450`) is TWO sentences, not one: the container
    clause ("both exit 0 inside a `--network=none` container") belongs only to the FIRST sentence,
    which names `bazel build`/`bazel test`; the SECOND sentence — "And the tests survived the
    move: for every repo whose `repos.baseline_ok = 1`, `bazel query 'tests(//<dest>/...)' | wc
    -l` is `>= repos.baseline_test_count`..." — starts fresh with "And" and carries no container
    clause of its own. So the query genuinely being exercised as PART OF a sandboxed `fleet build`
    invocation (same payload, same repo, same worker, real Docker for the build/test steps either
    side of it) — which this test proves — already satisfies the literal text; the query process
    itself does not additionally need to run inside the container's own network namespace. `D118`
    (see `docs/INTEGRATION_HONESTY.md`) is what this test's discriminator below actually needed
    fixed: the host-side query was passing the SANDBOXED cache-flag paths under a sandboxed
    payload, breaking it on the one host filesystem where `/cache` does not exist.
    """
    add_repos(fleet, ["acme-widgets-py"])
    _add_local_repo(fleet, "task57-happy-py", _TASK57_HAPPY_REPO)
    _add_local_repo(fleet, "task57-shrink-py", _TASK57_SHRINK_REPO)

    # -- Phase A: unsandboxed, warms the cache (build AND test) and publishes a REAL lock. --
    phase_a = real_build(
        fleet,
        monorepo,
        bazel_cache_home,
        bazel_registry,
        bazel_fetch_bazelrc,
        "--repo",
        "acme-widgets-py",
    )
    assert phase_a.exit_code == ExitCode.SUCCESS, phase_a.output

    lock_on_integration = git(monorepo, "show", f"integration:{MODULE_LOCK_PATH}")
    assert lock_on_integration.strip(), (
        "Phase A published no MODULE.bazel.lock onto `integration` — `_publish_module_lock` "
        "never captured a real Bazel-written lock, which is the leg this task exists to prove"
    )
    registry_findings = query(
        fleet,
        "SELECT kind, payload FROM findings WHERE kind = 'ModuleLockForeignRegistry'",
    )
    assert not registry_findings, (
        f"Phase A's lock was rejected as foreign-registry-keyed: {registry_findings}"
    )

    # -- D116, seeded and disclosed (see docstring). --
    conn = sqlite3.connect(fleet / "state" / "fleet.db", isolation_level=None)
    try:
        conn.execute(
            "UPDATE repos SET baseline_ok = 1, baseline_test_count = 1 "
            "WHERE repo_id = 'task57-happy-py'"
        )
        conn.execute(
            "UPDATE repos SET baseline_ok = 1, baseline_test_count = 99 "
            "WHERE repo_id = 'task57-shrink-py'"
        )
    finally:
        conn.close()

    # -- Phase B, happy path: sandboxed, real docker, over `task57-happy-py`. --
    phase_b_happy = build(fleet, "--repo", "task57-happy-py")
    assert phase_b_happy.exit_code == ExitCode.SUCCESS, phase_b_happy.output

    happy_attempts = [row for row in attempts(fleet, 3) if row["repo_id"] == "task57-happy-py"]
    happy_docker_rows = [
        row for row in happy_attempts if row["command"] and row["command"][0] == "docker"
    ]
    assert happy_docker_rows, f"no docker invocation recorded for task57-happy-py: {happy_attempts}"
    for row in happy_docker_rows:
        assert row["exit_code"] == 0, row
        assert "--network=none" in row["command"], row["command"]

    happy_row = query(
        fleet,
        "SELECT baseline_test_count, baseline_ok, migrated_test_count FROM repos "
        "WHERE repo_id = 'task57-happy-py'",
    )[0]
    happy_baseline, happy_baseline_ok, happy_migrated = happy_row
    assert happy_baseline_ok == 1, happy_row
    assert happy_migrated is not None and happy_migrated >= 1, (
        f"§12.11's whole point: a real `bazel query 'tests(//...)'` over a real sandboxed build "
        f"must find a genuinely non-vacuous count, not 0: {happy_row}"
    )
    assert happy_migrated >= happy_baseline, happy_row

    # -- Phase B, the discriminator: sandboxed, real docker, over `task57-shrink-py`. --
    phase_b_shrink = build(fleet, "--repo", "task57-shrink-py")
    assert phase_b_shrink.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION == 7, (
        phase_b_shrink.output
    )

    shrink_attempts = [
        row for row in attempts(fleet, 3) if row["repo_id"] == "task57-shrink-py"
    ]
    shrink_docker_rows = [
        row for row in shrink_attempts if row["command"] and row["command"][0] == "docker"
    ]
    assert shrink_docker_rows, (
        f"no docker invocation recorded for task57-shrink-py: {shrink_attempts}"
    )
    for row in shrink_docker_rows:
        assert "--network=none" in row["command"], row["command"]
    # The OLD boolean check's own evidence: `bazel build` AND `bazel test` both really exited 0
    # under real Docker — a real test target ran and passed — so `no_test_targets`/`tests_lost`
    # stays green throughout. If every docker row here were NOT exit 0, this discriminator would
    # be proving nothing (a build/test failure, not a shrink, would be the reason for RHI).
    assert all(row["exit_code"] == 0 for row in shrink_docker_rows), (
        f"a real build/test failure, not the count-shrink discriminator, is what failed this "
        f"repo — the mutation-proof shape requires the OLD check to stay green: "
        f"{shrink_docker_rows}"
    )
    shrink_row = query(
        fleet,
        "SELECT baseline_test_count, baseline_ok, migrated_test_count FROM repos "
        "WHERE repo_id = 'task57-shrink-py'",
    )[0]
    shrink_baseline, shrink_baseline_ok, shrink_migrated = shrink_row
    assert shrink_baseline_ok == 1, shrink_row
    assert shrink_migrated == 1, (
        f"the real `bazel query` must find exactly the one real test target this fixture "
        f"declares, over a real sandboxed build: {shrink_row}"
    )
    assert shrink_migrated < shrink_baseline, (
        f"the discriminator requires migrated < baseline, over REAL numbers: {shrink_row}"
    )
    # The `test_count_regressed` refusal is a WORKER-level comparison failure, not a subprocess
    # step — it never produces its own `attempts` row (the two rows above, both real `docker`
    # invocations, are the build and test steps, and both genuinely exited 0). Its record is
    # `phases.failure_class`/`phases.last_error`, written by the driver from the same
    # `WorkerError` `buildverify.py`'s `test_count_regressed` branch returns.
    shrink_phase = query(
        fleet,
        "SELECT failure_class, last_error FROM phases "
        "WHERE repo_id = 'task57-shrink-py' AND phase = 3",
    )[0]
    shrink_failure_class, shrink_last_error = shrink_phase
    assert shrink_failure_class == "TEST_FAILURE", shrink_phase
    assert "fewer than" in str(shrink_last_error), (
        f"the failure's own recorded reason must name the shrink, not just fail silently: "
        f"{shrink_phase}"
    )
