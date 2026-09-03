"""SPEC §12.34 Clause A: a fixture "Ruby" `EcosystemAdapter` + `ManifestAdapter` pair proves the
SPEC §1 new-language touchpoint count end to end, driven through a real
`scan → sequence → transform → build` run — with **zero** `src/fleet/` changes.

**Clause A only — do not read a green run here as §12.34 fully closed.** §12.34's SECOND sentence
(a `ContractBindingUnavailable` finding + `BuildPlan.unbound_contract_kinds` entry for a
`ContractKind` the fixture's `contract_bindings` omits) is Clause B, and is deliberately NOT
attempted by this file: research-23's report (round VI) found no `src/fleet/` driver call site
exists yet to exercise it — `workers/buildgen.py` never calls `ContractAdapter.neutral_targets`/
`binding_target`, so there is nothing for a fixture to drive. Clause B needs its own dedicated,
later task that first writes that production wiring.

**A strictly stronger, disclosed zero-diff claim.** §12.34's literal text asks for `git diff
--stat` over `src/fleet/` showing zero changes OUTSIDE `src/fleet/models/enums.py` (the "one
`Ecosystem` member" touchpoint). This fixture never adds a real `Ecosystem` member at all — per
the criterion's own "injected via the test's enum-extension fixture" phrasing, the member is a
decoy `Ecosystem.RUBY` this file builds at test time (`test_ecosystems.py`'s established
`test_discover_raises_naming_a_decoy_ecosystem_member_with_no_adapter` pattern), never written to
`enums.py`. The actually-achievable, and actually asserted, claim is the STRONGER one: zero
changed files in `src/fleet/` AT ALL — see
`test_ruby_fixture_touchpoints_require_zero_src_fleet_changes` below, which pins a fixed,
already-landed commit range (`test_local_profile_e2e.py::test_switching_profiles_requires_zero_
src_edits` is the template this repeats, per round VI task 35's repair of the same pattern), never
`HEAD`/working-tree.

**The judgment call the SPEC does not resolve (Agent Recommendation, per this project's CLAUDE.md
"Directive Authority" — not a directive):** `tests/fixtures/adapters/ruby_manifest.py` and
`ruby_ecosystem.py` need to reference the decoy `Ecosystem.RUBY` member, which does not exist at
their own import time (it is built by THIS test, after the fixture modules already imported).
Chosen: the **factory pattern** research-23's report recommends — each fixture module exports a
`make_ruby_*_adapter(ruby_member)` function that returns a freshly bound subclass, called here
after the decoy is built, with the result registered by this test. Chosen over the report's
late-binding alternative (a mutable class-level slot the test monkeypatches after the fact)
because it keeps the fixture files free of test-only monkeypatch mechanics of their own, and
matches research-23's own live-verified proof script, which built its inline fixture class the
same way — fully formed, inside the already-patched scope.

**A correction to research-23's report, found and verified by this task, not carried over from
it:** the report's Q3 "verified live" registration order — `reset_adapters()`, THEN register the
fixture, THEN one `discover(force=True)` call — only works in a FRESH interpreter, where the six
real adapter modules have never been imported before. In a real pytest session (this file's own
situation: `fleet.ecosystems.cargo` etc. are already in `sys.modules` from earlier tests),
`discover()`'s `reload = force and not _BY_ECOSYSTEM` is `False` once the fixture is registered
first (the registry is non-empty), so the six real modules take the `importlib.import_module`
no-op branch instead of `importlib.reload` and are silently NOT re-registered after the reset —
`discover(force=True)` then raises naming the six real ecosystems as missing, not "ruby" as
research-23's report describes. Verified two ways: reproduced the failure with the report's exact
order inside an already-warm interpreter, and found the fix — do not call `reset_adapters()` at
all; register the fixture ADDITIVELY on top of the registry `fleet.cli` already populated, so
`discover(force=True)` never needs to reload anything. `_decoy_ruby_ecosystem` below implements
the corrected order.
"""

from __future__ import annotations

import subprocess
import sys
import typing
from collections.abc import Iterator
from concurrent.futures import Executor, ThreadPoolExecutor
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from fleet import cli
from fleet.cli import ExitCode
from fleet.ecosystems import base as ecosystems_base
from fleet.manifests import base as manifests_base
from fleet.models.base import FleetModel
from fleet.models.enums import Ecosystem as RealEcosystem
from fleet.models.state import MigrationState
from fleet.orchestrator import budgets as budgets_module
from tests.fixtures.adapters.ruby_ecosystem import make_ruby_ecosystem_adapter
from tests.fixtures.adapters.ruby_manifest import make_ruby_manifest_adapter
from tests.test_build_e2e import (
    FakeBazel,
    FakeFilterRepo,
    FakeGazelle,
    FakeResolver,
    build,
    build_worktree,
    make_monorepo,
)
from tests.test_cli import MODELS_YAML
from tests.test_scan_e2e import _fresh_db, _make_repo
from tests.test_transform_e2e import query, scan, sequence, transform, write_rules

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: `main` tip this task branched from — the fixed lower endpoint of the zero-src-diff range
#: below. Verified with `git merge-base HEAD main` at the time this file was written.
_BASE_SHA = "016dc7710e5c519808d792d8a9ea491ae0550d46"

#: This task's own landing commit (round VI task 39, `f17e836` — added the fixture pair and this
#: test file). A SEPARATE, immediately-following commit corrects this constant to the real value,
#: since a commit cannot name its own hash (chicken-and-egg), exactly as
#: `test_local_profile_e2e.py`'s own `_HEAD_SHA` comment documents round VI task 35 doing for the
#: same pattern. Never left open-ended (`HEAD`/working-tree) — see that file's docstring for why.
_HEAD_SHA = "f17e836613e9da7169d975995f74646f9642a76d"

_REPO_NAME = "acme-gem"

_RUBY_FIXTURE_FILES: dict[str, str] = {
    "Gemfile": 'source "https://rubygems.org"\ngem "railties"\n',
    "lib/acme_gem.rb": ('module AcmeGem\n  def self.hello\n    "hi"\n  end\nend\n'),
}

#: `run.monorepo_path` MUST agree with `make_monorepo`'s own hardcoded `workspace.parent /
#: "acme-monorepo"` (`tests/test_build_e2e.py`), which this file reuses unmodified.
_FLEET_YAML = """\
run:
  monorepo_path: ../acme-monorepo
  cache_dir: cache/
  work_dir: work/
graph:
  hoist_contracts: false
concurrency:
  cpu_pool_workers: 1
  docker: 1
verify:
  container_memory: 64m
budgets:
  max_rss_mb: 512
build:
  ruleset_versions:
    rules_ruby: "3.5.0"
preflight:
  min_free_bytes: 1048576
"""
#: `rules_ruby` has no default `build.ruleset_versions` pin (`settings.py::BuildSection`) — every
#: shipped ruleset does, and `render_module_bazel` hard-errors on a `WorkspaceDep.ruleset` with no
#: pin, so this fixture needs its own, exactly as a real Ruby integration would (§9). No rule
#: files are written and `transform.engines` is left at its shipped default: this fixture proves
#: zero of §1's optional touchpoint 4 (import rewriting) and needs no rewrite engine to do it —
#: the shipped engine modules (`fleet.rewrite.astgrep` etc.) import cleanly with no external tool
#: on PATH (they raise only when a rule actually selects them, per their own docstrings), so
#: nothing here needs to override or fake one.


def _write_config(root: Path, sources: dict[str, Path]) -> None:
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "fleet.yaml").write_text(_FLEET_YAML, encoding="utf-8")
    (config / "models.yaml").write_text(MODELS_YAML, encoding="utf-8")
    # D21/§11.4: `redaction.history_scrub_file` defaults to `config/rules/secrets.txt`, and Phase
    # 3 ingest refuses to proceed with a non-empty configured path that is not on disk — the same
    # rule every other e2e fixture in this suite provisions (`tests/test_transform_e2e.py`'s own
    # `_write_config`), reused here verbatim for the same reason.
    rules_dir = config / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "secrets.txt").write_text(
        "regex:-----BEGIN [A-Z ]*PRIVATE KEY-----==>***REDACTED:private_key***\n",
        encoding="utf-8",
    )
    entries = "".join(f"  - name: {name}\n    url: {url}\n" for name, url in sources.items())
    (config / "repos.yaml").write_text(
        f"version: 1\ndefaults:\n  ref: main\nrepos:\n{entries}", encoding="utf-8"
    )
    # No `dest:` override anywhere in `repos.yaml` — the whole point of this fixture is that
    # `ruby/<gem>` comes out of `RubyEcosystemAdapter.monorepo_dir`/`path_tail()`, not a config
    # escape hatch.


def _make_ruby_workspace(tmp_path: Path) -> Path:
    sources = {_REPO_NAME: _make_repo(tmp_path / "sources", _REPO_NAME, _RUBY_FIXTURE_FILES)}
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_config(workspace, sources)
    write_rules(workspace)  # zero rule files: touchpoint 4 (import rewriting) is optional (§1)
    _fresh_db(workspace / "state" / "fleet.db")
    return workspace


def _fake_cpu_pool(max_workers: int) -> Executor:
    """`ThreadPoolExecutor` stand-in for `orchestrator.budgets.new_cpu_pool`, reused from
    `tests/test_local_profile_e2e.py::_fake_cpu_pool` (~5 lines; copied rather than imported
    cross-file per CLAUDE.md Rule 2 "simplicity first" — this is the only other user).

    Required, not optional (research-23 Q4): a real `cpu_pool` is a forkserver
    `ProcessPoolExecutor`; a spawned worker process is a fresh interpreter that never ran this
    test's `Ecosystem` patch or fixture registration, and would see the real, RUBY-less enum. A
    `grep -n cpu_pool src/fleet/workers/buildgen.py` finds no hits, but research-23's report is
    explicit that this is not a reason to skip the fakeout — applied unconditionally, as
    `test_local_profile_e2e.py` itself applies it "for the whole run, not just the LLM-calling
    phases".
    """
    return ThreadPoolExecutor(max_workers=max_workers)


def _install_fake_bazel_stack(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    monkeypatch.setattr(cli, "FILTER_REPO_RUNNER", FakeFilterRepo())
    monkeypatch.setattr(cli, "RESOLVER_RUNNER", FakeResolver())
    monkeypatch.setattr(cli, "GAZELLE_RUNNER", FakeGazelle())
    monkeypatch.setattr(cli, "BAZEL_RUNNER", FakeBazel(workspace / "artifacts" / "fake-bazel"))


# -------------------------------------------------------------------------------------------
# the decoy `Ecosystem.RUBY` mechanism — two layers, both patched and both fully restored
# -------------------------------------------------------------------------------------------

#: Every module whose OWN `from fleet.models.enums import Ecosystem` binding is read as a LIVE
#: global at runtime — an `Ecosystem(value)` call or a `for eco in Ecosystem` iteration — rather
#: than baked into a pydantic schema at import time. Found by an exhaustive grep of `src/fleet/`
#: for `in Ecosystem\b` and bare `Ecosystem(` call sites (this task's own verification, not
#: carried over from research-23's report, which flagged this exact question as open — "should be
#: checked by the implementer rather than assumed"). Just these three: `ecosystems.base.discover`'s
#: bijection loop, `manifests.base` (imports `Ecosystem` but only for its `ManifestAdapter.
#: ecosystem: ClassVar` type — no live use found, patched anyway since it is cheap and the
#: import site the brief explicitly flagged as unverified), and `cli.py`'s five direct
#: `Ecosystem(str(row))` reconstructions off DB text columns (`_repo_facts`, `_plan_build`,
#: two more).
_ECOSYSTEM_MODULE_BINDINGS: tuple[tuple[ModuleType, str], ...] = (
    (ecosystems_base, "Ecosystem"),
    (manifests_base, "Ecosystem"),
    (cli, "Ecosystem"),
)


def _mentions_ecosystem(annotation: object) -> bool:
    if annotation is RealEcosystem:
        return True
    return any(_mentions_ecosystem(arg) for arg in typing.get_args(annotation))


def _substitute_ecosystem(annotation: object, decoy: type) -> object:
    if annotation is RealEcosystem:
        return decoy
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if not args:
        return annotation
    new_args = tuple(_substitute_ecosystem(arg, decoy) for arg in args)
    if new_args == args:
        return annotation
    if hasattr(annotation, "copy_with"):
        return annotation.copy_with(new_args)
    assert origin is not None
    return origin[new_args]


@contextmanager
def _decoy_ruby_ecosystem() -> Iterator[RealEcosystem]:
    """Extends `Ecosystem` with one decoy `RUBY` member for the block's duration.

    Two layers, both patched here and both fully restored on exit (even on test failure):

    1. The three LIVE module bindings in `_ECOSYSTEM_MODULE_BINDINGS` above.
    2. Every `pydantic` model field, anywhere already imported under `fleet.*`, whose annotation
       mentions the REAL `Ecosystem` class — `Coordinate.ecosystem`, `BuildUnit.ecosystem`,
       `RepoRecord.ecosystems`, `ManifestRef.ecosystem`, `InterrogateOutput.ecosystems` and half a
       dozen more this task found by an exhaustive sweep rather than by manually enumerating call
       sites (a manual enumeration, tried first, missed `ClassifyInput`/`ClassifyOutput` and both
       `fleet.llm.schemas` models — they are only reachable once `fleet.cli` is fully imported,
       which this sweep requires as a precondition and a hand list would have to remember too).
       A `pydantic` model's enum field is validated against the literal choice list baked into its
       COMPILED core schema at class-definition time — patching the module attribute alone (layer
       1's mechanism) does not touch that compiled schema, proven by a live check before this was
       written (`Coordinate(ecosystem=<decoy>.RUBY, ...)` raises `ValidationError` even after
       `Coordinate.model_rebuild(force=True)` with no field override — pydantic's rebuild reads
       the type it already resolved, not the module's current attribute). Explicitly overriding
       `model_fields[name].annotation` before `model_rebuild(force=True)` is what makes it work.

    **Registration order, corrected from research-23's report** (see this file's module
    docstring): the two fixture adapters are registered ADDITIVELY, on top of whatever
    `fleet.ecosystems`/`fleet.manifests` already discovered — never after a `reset_adapters()` —
    because a reset immediately followed by one `discover(force=True)` only re-registers the six
    real adapters when their modules have never been imported before in this process, which is
    false by the time any other test in this suite has run. Verified working end to end,
    including a teardown that fully restores the six-real, zero-ruby registry (this function's
    `finally` block), by a live reproduction kept out of the committed tree per this project's
    CLAUDE.md ("script deleted after use") convention research-23's own Q3 already established.
    """
    decoy = StrEnum(  # type: ignore[misc]  # dynamic member list; mypy needs a literal
        "Ecosystem", [(m.name, m.value) for m in RealEcosystem] + [("RUBY", "ruby")]
    )
    ruby_member: RealEcosystem = getattr(decoy, "RUBY")  # noqa: B009 (mypy can't see decoy's members)

    mp = pytest.MonkeyPatch()
    for module, attr in _ECOSYSTEM_MODULE_BINDINGS:
        mp.setattr(module, attr, decoy)  # raising=True is the default -- exactly the fix

    saved_fields: list[tuple[type[FleetModel], str, Any]] = []
    rebuilt: set[type[FleetModel]] = set()
    for mod_name, module in list(sys.modules.items()):
        if not mod_name.startswith("fleet"):
            continue
        for attr_name in dir(module):
            obj = getattr(module, attr_name, None)
            if not isinstance(obj, type) or not issubclass(obj, FleetModel):
                continue
            if obj is FleetModel or obj in rebuilt:
                continue
            changed = False
            for field_name, field_info in obj.model_fields.items():
                if _mentions_ecosystem(field_info.annotation):
                    saved_fields.append((obj, field_name, field_info.annotation))
                    substituted = _substitute_ecosystem(field_info.annotation, decoy)
                    field_info.annotation = typing.cast("type[Any] | None", substituted)
                    changed = True
            if changed:
                rebuilt.add(obj)
    for model_cls in rebuilt:
        model_cls.model_rebuild(force=True)

    manifests_base.register(make_ruby_manifest_adapter(ruby_member))
    ecosystems_base.register(make_ruby_ecosystem_adapter(ruby_member))
    manifests_base.discover(force=True)
    ecosystems_base.discover(force=True)

    try:
        yield ruby_member
    finally:
        for model_cls, field_name, original in saved_fields:
            model_cls.model_fields[field_name].annotation = original
        for model_cls in rebuilt:
            model_cls.model_rebuild(force=True)
        mp.undo()
        ecosystems_base.reset_adapters()
        manifests_base.reset_adapters()
        ecosystems_base.discover(force=True)
        manifests_base.discover(force=True)


# -------------------------------------------------------------------------------------------
# 1. the mechanical assertion — the fixture required zero `src/fleet/` edits, not just zero
#    outside `enums.py` (the SPEC's literal, weaker text — see module docstring)
# -------------------------------------------------------------------------------------------


def test_ruby_fixture_touchpoints_require_zero_src_fleet_changes() -> None:
    """§12.34 Clause A's own diff, over the FIXED historical range `_BASE_SHA.._HEAD_SHA` this
    task's commit landed in — never `HEAD`/working-tree (`test_local_profile_e2e.py`'s
    `test_switching_profiles_requires_zero_src_edits` is the template, including the exact
    regression round VI task 35 repaired: an open-ended second endpoint fails on every later,
    unrelated `src/` commit forever after)."""
    result = subprocess.run(  # noqa: S603
        ["git", "diff", "--stat", _BASE_SHA, _HEAD_SHA, "--", "src/fleet/"],  # noqa: S607
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == "", (
        "§12.34 Clause A's fixture Ruby adapter pair was supposed to need ZERO src/fleet/ edits "
        f"(stronger than the SPEC's literal 'zero outside enums.py' — see module docstring), but "
        f"the diff {_BASE_SHA}..{_HEAD_SHA} (src/fleet/ only) is non-empty:\n{result.stdout}"
    )


# -------------------------------------------------------------------------------------------
# 2. the end-to-end drive: scan -> sequence -> transform -> build, over a fixture repo no real
#    Ecosystem member has ever recognized
# -------------------------------------------------------------------------------------------


def test_ruby_fixture_scans_sequences_transforms_and_builds_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC §12.34 Clause A, driven for real: a fixture Ruby repo scans, sequences, relocates to
    `ruby/<gem>`, and Phase 3 emits a `ruby_library` `BuildTarget` and a `rules_ruby`
    `WorkspaceDep` — with the repo showing up in `migration_state.json`."""
    with _decoy_ruby_ecosystem():
        workspace = _make_ruby_workspace(tmp_path)
        monkeypatch.chdir(workspace)
        monkeypatch.setattr(budgets_module, "new_cpu_pool", _fake_cpu_pool)
        monkeypatch.setattr(cli, "new_cpu_pool", _fake_cpu_pool)

        assert scan(workspace).exit_code == ExitCode.SUCCESS
        assert sequence(workspace).exit_code == ExitCode.SUCCESS
        assert transform(workspace).exit_code == ExitCode.SUCCESS

        # -- Phase 1: the repo really was classified as the fixture's own ecosystem, not UNKNOWN --
        ecosystems_json = query(
            workspace, "SELECT ecosystems FROM repos WHERE repo_id = ?", (_REPO_NAME,)
        )
        assert ecosystems_json == [('["ruby"]',)], ecosystems_json

        make_monorepo(workspace)
        _install_fake_bazel_stack(monkeypatch, workspace)
        result = build(workspace, "--no-sandbox")
        assert result.exit_code == ExitCode.SUCCESS, result.output

        # -- relocation: ruby/<gem>, via RubyEcosystemAdapter.monorepo_dir/path_tail(), no dest: --
        dest_rows = query(workspace, "SELECT dest_path FROM repos WHERE repo_id = ?", (_REPO_NAME,))
        dest = f"ruby/{_REPO_NAME}"
        # `dest_path` stays NULL in the DB for an adapter-computed destination (no `dest:`
        # override was ever written) — the worktree path below is the real proof of `ruby/<gem>`.
        assert dest_rows == [(None,)], dest_rows

        worktree = build_worktree(workspace, _REPO_NAME)
        assert (worktree / dest).is_dir(), sorted(p.name for p in worktree.iterdir())

        # -- a ruby_library BuildTarget --------------------------------------------------------
        build_bazel = (worktree / dest / "BUILD.bazel").read_text(encoding="utf-8")
        assert "ruby_library(" in build_bazel, build_bazel
        assert '"@rules_ruby//ruby:defs.bzl"' in build_bazel, build_bazel

        # -- a rules_ruby WorkspaceDep, rendered into MODULE.bazel -----------------------------
        module_bazel = (worktree / "MODULE.bazel").read_text(encoding="utf-8")
        assert 'bazel_dep(name = "rules_ruby"' in module_bazel, module_bazel
        assert "gem.install" in module_bazel, module_bazel

        # -- migration_state.json: the repo is neither dropped nor a crash ---------------------
        projection = workspace / "migration_state.json"
        state = MigrationState.model_validate_json(projection.read_text(encoding="utf-8"))
        assert _REPO_NAME in state.repos, sorted(state.repos)
        assert state.repos[_REPO_NAME].status.value == "SUCCEEDED", state.repos[_REPO_NAME]
