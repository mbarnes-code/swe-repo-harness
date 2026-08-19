"""Phase 4 steps 4–5 end to end, and the `CycleDetected` findings §11.6 believed it already had.

This file continues `tests/test_build_e2e.py`'s five real repositories through `fleet pr`, and it
exists for one defect above all others. SPEC §3.4 step 5: *"`MERGED` is a fact about GitHub, and
until something reads it back nothing in this spec ever writes it — which would deadlock the fleet
at the first wave boundary."* Three gates consume `PrState.MERGED` and, before this wiring, no
code path produced it: wave 0's PRs opened, nothing polled the forge, and every later wave's
Phase 4 precondition failed forever. On a 250-repo run that strands 242 repos behind 8 that had
actually landed. The headline test below therefore does not assert that a column was written — it
asserts that a repo which `fleet pr` HELD before the sync is SHIPPED after it, and that the repo
whose dependency was *not* merged is still held in the same invocation.

**Read this before believing any assertion here.** `gh` is NOT installed on this host and this
harness does not install it, so the split is stated rather than implied:

* *Proven by real execution.* Everything except the four `gh` subprocesses: the chain
  `scan → sequence → transform → build → verify → pr` over real git repositories; the wave gate;
  the eligibility rule read off ingested state; the `findings` rows that carry `PullRequestDraft`
  and `VerificationReport`; the `events` row `pr_merged`; the PR body, which is written to a real
  file on disk by the shipped `render_body` and asserted from that file; `phases.pr_url`; the
  `CycleDetected` rows; and `MigrationState.cycles` through the real `state/projection.py`.
* *Proven only against an injected runner.* The `gh` process itself. What is real about it is the
  argv, the JSON parsing (`vcs/github.parse_pr_view`, unchanged and pure), the exit-code handling
  and every state transition downstream; what is not real is that GitHub ever saw a PR.
  `cli.GH_RUNNER` is the seam, it is `None` in production, and `FakeForge` answers `gh pr view`
  from a table the *test* mutates — which is what lets "a human merged it" be a fact the harness
  discovers rather than one it is handed.

The seam is deliberately narrow: it never reaches `Git`, so every commit, branch and merge these
tests stand on really happened.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from typer.testing import CliRunner

from fleet import cli
from fleet.cli import ExitCode, app
from fleet.llm import client as client_module
from fleet.llm.client import StructuredOutputMode, TransportError
from fleet.models.enums import BreakStrategy, PrState
from fleet.state.db import connect_ro
from fleet.state.projection import build_state
from fleet.util.proc import ProcResult
from fleet.workers.prwriter import STUB_BANNER
from tests.test_build_e2e import (  # noqa: F401  (fixtures are used by injection)
    FakeBazel,
    FakeFilterRepo,
    bazel,
    build,
    filter_repo,
    gazelle,
    make_monorepo,
    monorepo,
    resolver,
    verify,
)
from tests.test_scan_e2e import _git
from tests.test_transform_e2e import (  # noqa: F401  (fixtures are used by injection)
    base_args,
    fleet,
    query,
    scan,
    scanned,
    sequence,
    transform,
)

runner = CliRunner()

#: The fixture fleet's ordering, which is what makes the stacking assertions meaningful:
#: `acme-app-*` declares a dependency on `acme-lib-*`, so wave 0 is the two libraries and wave 1
#: the two applications. A PR for an application may not open until its library's PR is MERGED.
LIBRARIES = ("acme-lib-py", "acme-lib-ts")
APPLICATIONS = ("acme-app-py", "acme-app-ts")

STUB_COORD = "npm:@acme/gone"

#: `sha256(canonical_json([]))` — what the §11.6 `cycles` section hashed to for every run before
#: `fleet sequence` persisted a single `CycleDetected` row. Named so the digest test cannot pass
#: by comparing two empty sections to each other.
EMPTY_SECTION_DIGEST = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"


# ---------------------------------------------------------------------------------------
# the injected `gh` — the ONLY thing in this file that is not really executed
# ---------------------------------------------------------------------------------------


class FakeForge:
    """`gh`, recorded and answered from a table the test owns.

    The table is the point. `merge()` is the test standing in for a human clicking "Merge" on
    GitHub, and nothing in the harness is told about it — the harness has to *discover* it through
    `gh pr view`, which is precisely the step §3.4 says must exist. A fake that reported `MERGED`
    the moment a PR was created would have proven the opposite of what this file is for.

    Everything the harness does with the answer is shipped code: `parse_pr_view` (which RAISES on
    an unknown state rather than defaulting to OPEN), `PrStatus`, the eligibility rule, the
    `findings` upsert and the `pr_merged` event.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.cwds: list[Path | None] = []
        self.state: dict[str, str] = {}
        self._seq = 0

    # -- the forge side, driven by the test --------------------------------------------

    def url_for(self, repo_id: str) -> str:
        return f"https://github.invalid/acme/monorepo/pull/{repo_id}"

    def merge(self, *repo_ids: str) -> None:
        """A human merged these PRs. The harness is told nothing; it must poll to find out."""
        for repo_id in repo_ids:
            self.state[self.url_for(repo_id)] = "MERGED"

    # -- the recorded views a test asserts on ------------------------------------------

    def commands(self, *prefix: str) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[1 : 1 + len(prefix)] == prefix]

    def created(self) -> dict[str, tuple[str, ...]]:
        """`head branch -> argv` for every `gh pr create`."""
        out: dict[str, tuple[str, ...]] = {}
        for call in self.commands("pr", "create"):
            out[call[call.index("--head") + 1]] = call
        return out

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
        call = tuple(argv)
        self.calls.append(call)
        self.cwds.append(cwd)
        match call[1:]:
            case ("auth", "status"):
                return self._result(call, stdout="Logged in to github.invalid\n")
            case ("pr", "create", *_rest):
                return self._create(call)
            case ("pr", "view", url, "--json", _fields):
                return self._result(call, stdout=self._view(url))
            case ("pr", "ready", _url):
                return self._result(call)
            case _:  # pragma: no cover - an unrecognised argv is a test bug, loudly
                raise AssertionError(f"unexpected gh invocation: {call}")

    def _create(self, call: tuple[str, ...]) -> ProcResult:
        repo_id = call[call.index("--head") + 1].removeprefix("migrate/")
        url = self.url_for(repo_id)
        if url in self.state:  # pragma: no cover - a duplicate PR is a test failure, not a fixture
            raise AssertionError(f"gh pr create called twice for {repo_id}")
        self.state[url] = "OPEN"
        self._seq += 1
        return self._result(call, stdout=f"Creating pull request…\n{url}\n")

    def _view(self, url: str) -> str:
        state = self.state.get(url, "OPEN")
        payload: dict[str, object] = {"state": state, "mergedAt": None, "mergeCommit": None}
        if state == "MERGED":
            payload["mergedAt"] = "2026-08-09T12:00:00Z"
            payload["mergeCommit"] = {"oid": "f" * 40}
        return json.dumps(payload)

    def _result(self, call: tuple[str, ...], *, stdout: str = "") -> ProcResult:
        return ProcResult(
            argv=call,
            exit_code=0,
            stdout_tail=stdout,
            stderr_tail="",
            duration_ms=3,
            timed_out=False,
        )


@pytest.fixture
def forge(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeForge]:
    """The `gh` seam, installed for the life of one test and `None` again afterwards."""
    fake = FakeForge()
    monkeypatch.setattr(cli, "GH_RUNNER", fake)
    yield fake


# ---------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------


def run_pr(root: Path, *extra: str) -> Any:
    return runner.invoke(
        app, [*base_args(root), "--json", "pr", *extra], catch_exceptions=False
    )


def digest_of(root: Path) -> dict[str, Any]:
    result = runner.invoke(
        app, [*base_args(root), "--json", "status", "--digest"], catch_exceptions=False
    )
    assert result.exit_code == ExitCode.SUCCESS, result.output
    return dict(json.loads(result.stdout))


def payload(result: Any) -> dict[str, Any]:
    return dict(json.loads(result.stdout))


def verified(root: Path) -> None:
    """Phases 1–4, all asserted. A `fleet pr` test that started from a broken verification would
    report step 4's failure for step 3's reason."""
    scanned(root)
    assert transform(root).exit_code == ExitCode.SUCCESS
    assert build(root, "--no-sandbox").exit_code == ExitCode.SUCCESS
    assert verify(root).exit_code == ExitCode.SUCCESS


def run_id_of(root: Path) -> str:
    return str(query(root, "SELECT run_id FROM runs")[0][0])


def pr_states(root: Path) -> dict[str, str]:
    """`repo_id -> PrState`, read back out of the persisted `PullRequestDraft` payloads."""
    rows = query(
        root, "SELECT repo_id, payload FROM findings WHERE kind = 'PullRequest' ORDER BY repo_id"
    )
    return {str(row[0]): str(json.loads(str(row[1]))["state"]) for row in rows}


def body_of(root: Path, repo_id: str) -> str:
    """The PR body the harness really wrote to disk, read from the `--body-file` path."""
    path = root / "work" / "pr" / "bodies" / run_id_of(root) / f"{repo_id}-0-pr-body.md"
    return path.read_text(encoding="utf-8")


def degrade(root: Path, repo_id: str) -> None:
    """Put one repo in the §3.5 escape hatch: DEGRADED, with a live `ACTIVE` stub row.

    Written straight to SQLite because `--stub-blocked` is refused by `fleet build` (no worker
    emits a stub in this tree), and refusing to test the draft/banner rule until that worker
    exists would leave §3.5.1's most consequential PR rule unproven.
    """
    run_id = run_id_of(root)
    conn = sqlite3.connect(root / "state" / "fleet.db")
    try:
        conn.execute(
            "UPDATE phases SET status = 'DEGRADED', stubbed_deps = ? "
            " WHERE run_id = ? AND repo_id = ? AND phase = 4",
            (json.dumps([STUB_COORD]), run_id, repo_id),
        )
        conn.execute(
            "INSERT INTO stubs (stub_id, run_id, repo_id, stub_coord_key, consumer_repo_id, "
            "                   provider_repo_id, pinned_version, bazel_label, state, "
            "                   stub_fidelity, state_changed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'acme-empty', '1.2.3', ?, 'ACTIVE', 'PUBLISHED_ARTIFACT', "
            "        ?, ?)",
            (
                "stub-gone",
                run_id,
                repo_id,
                STUB_COORD,
                repo_id,
                f"//third_party/stubs/{STUB_COORD}",
                "2026-08-09T00:00:00+00:00",
                "2026-08-09T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def plant_cycle(root: Path) -> None:
    """Make `acme-lib-ts` declare a dependency on `acme-app-ts`, closing a real 2-repo SCC.

    Planted in the SOURCE manifest and committed, so the cycle is inferred by the shipped scanner
    off a real `package.json` rather than injected into the `edges` table — a cycle the harness
    did not derive is not a cycle this file can claim it broke.
    """
    source = root.parent / "sources" / "acme-lib-ts"
    manifest = json.loads((source / "package.json").read_text(encoding="utf-8"))
    manifest["dependencies"] = {"@acme/app": "^0.2.0"}
    (source / "package.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "-m", "declare a cyclic dependency")


def cycles_through_the_projection(root: Path) -> list[Any]:
    """`MigrationState.cycles`, via the real `state/projection.py` — never by reading the table."""

    async def read() -> list[Any]:
        conn = await connect_ro(root / "state" / "fleet.db")
        try:
            state = await build_state(conn, UUID(run_id_of(root)))
        finally:
            await conn.close()
        return list(state.cycles)

    return asyncio.run(read())


# ---------------------------------------------------------------------------------------
# 1. step 4 — one PR per eligible repo, with the argv, base and body §3.4 specifies
# ---------------------------------------------------------------------------------------


def test_pr_opens_one_pr_per_eligible_repo_with_the_argv_base_and_body_spec_names(
    fleet: Path, monorepo: Path, bazel: FakeBazel, forge: FakeForge  # noqa: F811
) -> None:
    """`fleet pr` really composes `PrwriterWorker`, and the PR it opens is the one §3.4 describes.

    Why the argv and the body are asserted rather than the return code: the verb's whole product
    is a PR a human reviews, and the two things that make it reviewable are mechanical — it must
    target the monorepo's integration branch (`--base integration`, `--head migrate/<repo>`, the
    ADR-0011 stack) and its body must carry the verdict fields §3.4 lists, rendered from the
    `VerificationReport` rather than from a model. A `fleet pr` that returned 0 while opening a PR
    against the wrong base, or with a body a model wrote, would pass a status-code assertion and
    fail every reviewer.

    The body is read back from the file the harness wrote, not from the argv: §3.4 sends it
    through `--body-file` precisely so a body quoting build logs and repo URLs never reaches the
    process table.
    """
    verified(fleet)

    result = run_pr(fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output
    body = payload(result)

    # Wave 0 ships; wave 1 cannot, because no dependency PR is MERGED yet.
    assert sorted(body["opened"]) == list(LIBRARIES), body
    created = forge.created()
    assert sorted(created) == [f"migrate/{repo}" for repo in LIBRARIES], created

    argv = created["migrate/acme-lib-ts"]
    assert argv[:2] == ("gh", "pr"), argv
    assert argv[2] == "create", argv
    assert argv[argv.index("--base") + 1] == "integration", argv
    assert argv[argv.index("--head") + 1] == "migrate/acme-lib-ts", argv
    assert "--body-file" in argv, "§3.4 sends the body through a file, never through argv"
    assert "--draft" not in argv, "a FULL verification is not a disclosed reduction (§3.4)"
    # The file named on the argv is the file the harness wrote.
    assert Path(argv[argv.index("--body-file") + 1]).read_text(encoding="utf-8") == body_of(
        fleet, "acme-lib-ts"
    )

    rendered = body_of(fleet, "acme-lib-ts")
    assert "## Migration of `acme-lib-ts` (wave 0)" in rendered, rendered
    assert "- Base: `integration` ← `migrate/acme-lib-ts`" in rendered, rendered
    assert "- Equivalence: `FULL`" in rendered, rendered
    assert "- Verdict: **PASS**" in rendered, rendered
    assert "- none — this node is a graph source" in rendered, rendered

    # The url is persisted where §3.4's success criterion says it must be resolvable from.
    urls = dict(query(fleet, "SELECT repo_id, pr_url FROM phases WHERE phase = 4"))
    assert urls["acme-lib-ts"] == forge.url_for("acme-lib-ts"), urls
    assert pr_states(fleet)["acme-lib-ts"] == PrState.OPEN.value, pr_states(fleet)


# ---------------------------------------------------------------------------------------
# 2. THE HEADLINE — step 5 ingests `MERGED`, and the gate that was blocked on it opens
# ---------------------------------------------------------------------------------------


def test_pr_sync_ingests_merge_state_and_a_gate_blocked_on_merged_becomes_satisfied(
    fleet: Path, monorepo: Path, bazel: FakeBazel, forge: FakeForge  # noqa: F811
) -> None:
    """`fleet pr --sync` is the difference between a fleet that finishes and one that deadlocks.

    Why this test is the point of the file. `MERGED` is a fact about GitHub. Three gates consume
    it — the §3.4 Phase 4 stacking precondition, the §3.5 `blocked_by` release and the §3.5.1 T1
    stub trigger — and before this wiring nothing in the harness produced it. Wave 0's PRs opened,
    nothing polled the forge, and every later wave was refused forever: 242 of 250 repos stranded
    behind 8 that had already landed.

    So the assertion is behavioural, and the control is the whole argument. `acme-app-py` is HELD.
    A human merges its dependency's PR. `fleet pr` run again — with the forge already reporting
    MERGED — STILL holds it, because the harness has not ingested anything and refuses to assume.
    Only after `fleet pr --sync` does the same command ship it. And in that same invocation
    `acme-app-ts`, whose dependency was never merged, is still held: the gate opened for a reason,
    not because a second run is more permissive than a first.
    """
    verified(fleet)
    first = run_pr(fleet)
    assert first.exit_code == ExitCode.SUCCESS, first.output
    assert payload(first)["held"] == {
        "acme-app-py": ["acme-lib-py"],
        "acme-app-ts": ["acme-lib-ts"],
    }, payload(first)

    # A human merges exactly one dependency PR. The harness is told nothing.
    forge.merge("acme-lib-py")

    blind = run_pr(fleet)
    assert blind.exit_code == ExitCode.SUCCESS, blind.output
    assert "acme-app-py" in payload(blind)["held"], (
        "the gate must read INGESTED state: an un-polled merge is not a merge (§3.4 step 5)"
    )
    assert not forge.commands("pr", "create")[2:], "nothing may be shipped on an unpolled merge"

    synced = run_pr(fleet, "--sync")
    assert synced.exit_code == ExitCode.SUCCESS, synced.output
    ingested = payload(synced)
    assert ingested["merged"] == ["acme-lib-py"], ingested
    assert ingested["unchanged"] == ["acme-lib-ts"], ingested
    assert pr_states(fleet)["acme-lib-py"] == PrState.MERGED.value, pr_states(fleet)

    # The event §3.4 says unblocks the dependent — emitted by the writer, not guessed by a worker.
    events = query(
        fleet, "SELECT repo_id, payload FROM events WHERE event = 'pr_merged' ORDER BY seq"
    )
    assert [row[0] for row in events] == ["acme-lib-py"], events
    assert json.loads(str(events[0][1]))["merge_commit_sha"] == "f" * 40, events

    after = run_pr(fleet)
    assert after.exit_code == ExitCode.SUCCESS, after.output
    shipped = payload(after)
    assert list(shipped["opened"]) == ["acme-app-py"], shipped
    assert shipped["held"] == {"acme-app-ts": ["acme-lib-ts"]}, shipped
    assert "migrate/acme-app-py" in forge.created(), forge.created()

    # The stacked PR names the dependency it waited for, in the state it was observed in.
    assert "- `acme-lib-py` — " in body_of(fleet, "acme-app-py")
    assert "(`MERGED`)" in body_of(fleet, "acme-app-py")


# ---------------------------------------------------------------------------------------
# 3. §3.5.1 — a DEGRADED repo is a draft with the banner, and is never promoted
# ---------------------------------------------------------------------------------------


def test_a_degraded_repos_pr_is_a_draft_with_the_stub_banner_and_is_never_marked_ready(
    fleet: Path, monorepo: Path, bazel: FakeBazel, forge: FakeForge  # noqa: F811
) -> None:
    """A stub is a lie with a known shape; the PR has to say so, and no flag may un-say it.

    §3.5 item 3 and §3.5.1: a repo that migrated against a generated stub is `DEGRADED`, its PR
    opens as a **draft** whose body leads with the verbatim banner and lists each stubbed
    coordinate with its fidelity tier, and `fleet pr --ready` refuses (exit 2) while any `stubs`
    row is `ACTIVE` or `SUPERSEDED`. That refusal is the whole safety property: a green build
    against a stub proves the code compiles against the stub's surface and nothing about the real
    dependency, so promotion is a human's decision after the stub lands — never the harness's.

    The negative assertion carries the weight: `gh pr ready` is never invoked and the persisted
    state stays `DRAFTED`, so a refusal that merely printed an error while promoting anyway would
    fail here.
    """
    verified(fleet)
    assert run_pr(fleet).exit_code == ExitCode.SUCCESS
    forge.merge(*LIBRARIES)
    assert run_pr(fleet, "--sync").exit_code == ExitCode.SUCCESS
    degrade(fleet, "acme-app-ts")

    result = run_pr(fleet, "--repo", "acme-app-ts")
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert payload(result)["draft"] == ["acme-app-ts"], payload(result)

    argv = forge.created()["migrate/acme-app-ts"]
    assert "--draft" in argv, argv

    rendered = body_of(fleet, "acme-app-ts")
    assert rendered.startswith(f"> **{STUB_BANNER}**"), rendered
    assert f"> - `{STUB_COORD}` — fidelity `PUBLISHED_ARTIFACT`" in rendered, rendered
    assert "- Equivalence: `STUB_LIMITED`" in rendered, rendered
    assert "- Draft: `True`" in rendered, rendered
    assert pr_states(fleet)["acme-app-ts"] == PrState.DRAFTED.value, pr_states(fleet)

    refused = run_pr(fleet, "--ready", "--repo", "acme-app-ts")
    assert refused.exit_code == ExitCode.USAGE, refused.output
    assert not forge.commands("pr", "ready"), forge.calls
    assert pr_states(fleet)["acme-app-ts"] == PrState.DRAFTED.value, pr_states(fleet)


# ---------------------------------------------------------------------------------------
# 4. ADR-0011 — an unmerged dependency holds its dependent rather than failing it
# ---------------------------------------------------------------------------------------


def test_a_repo_whose_dependency_pr_is_not_merged_is_held_and_never_shipped(
    fleet: Path, monorepo: Path, bazel: FakeBazel, forge: FakeForge  # noqa: F811
) -> None:
    """HELD is a first-class, pollable state — not a failure, and not a silent skip.

    §3.4's precondition is "every dependency's PR is `MERGED`". Where an unmet precondition lands
    decides whether a 250-repo run survives it: failing the repo would consume one of its three
    ADR-0014 attempts on someone else's review latency, and shipping it would put an application
    PR on a base that does not yet contain the library it imports. So the repo is reported, with
    the exact dependency that holds it, and `gh pr create` is never invoked for it — which is what
    `pr.merge_wait_timeout_s` later bounds rather than replaces.
    """
    verified(fleet)
    result = run_pr(fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output
    body = payload(result)

    assert sorted(body["held"]) == list(APPLICATIONS), body
    assert body["held"]["acme-app-ts"] == ["acme-lib-ts"], body
    assert body["failed"] == {}, "a held repo is not a failed repo"
    assert sorted(body["opened"]) == list(LIBRARIES), body

    for repo in APPLICATIONS:
        assert f"migrate/{repo}" not in forge.created(), forge.created()
        assert repo not in pr_states(fleet), pr_states(fleet)
    urls = dict(query(fleet, "SELECT repo_id, pr_url FROM phases WHERE phase = 4"))
    assert urls["acme-app-ts"] is None, urls


# ---------------------------------------------------------------------------------------
# 5. idempotency — §11.7 over a side effect that lives on a forge
# ---------------------------------------------------------------------------------------


def test_re_running_fleet_pr_opens_no_second_pr(
    fleet: Path, monorepo: Path, bazel: FakeBazel, forge: FakeForge  # noqa: F811
) -> None:
    """A duplicate PR is the one side effect this harness cannot roll back.

    Everything else `fleet` does is re-derivable — a commit is guarded by its trailer, a merge by
    `already_ingested()`, a `findings` row by its idempotency index. A second `gh pr create`
    leaves a second PR on a real forge with a real number that a human must close by hand, so
    re-entry is checked against the persisted record before any `gh` call is made, not after.
    """
    verified(fleet)
    assert run_pr(fleet).exit_code == ExitCode.SUCCESS
    creates = len(forge.commands("pr", "create"))
    assert creates == len(LIBRARIES), forge.calls

    again = run_pr(fleet)
    assert again.exit_code == ExitCode.SUCCESS, again.output
    assert sorted(payload(again)["already_open"]) == list(LIBRARIES), payload(again)
    assert payload(again)["opened"] == {}, payload(again)
    assert len(forge.commands("pr", "create")) == creates, forge.calls
    assert len(pr_states(fleet)) == len(LIBRARIES), pr_states(fleet)


# ---------------------------------------------------------------------------------------
# 6. `CycleDetected` — the findings two readers were already looking for
# ---------------------------------------------------------------------------------------


def test_sequence_writes_cycle_findings_and_the_projection_reports_them(
    fleet: Path,  # noqa: F811
) -> None:
    """`MigrationState.cycles` is non-empty, read through the real projection.

    `fleet sequence` has always computed a `CycleFinding` per non-trivial SCC — members, feedback
    edges, the 6c ladder's verdict, the edges 6d actually suppressed — and then dropped every one
    of them. `state/projection.py` reads `findings WHERE kind = 'CycleDetected'`, so
    `MigrationState.cycles` was empty in production and the projection could never answer the one
    question an operator asks about a repo that migrated oddly: *why*. Asserting through
    `build_state` rather than against the table is deliberate — the row's SHAPE is the contract,
    and a payload the model cannot re-validate would leave `cycles` empty exactly as before while
    a `SELECT COUNT(*)` said everything was fine.
    """
    plant_cycle(fleet)
    assert scan(fleet).exit_code == ExitCode.SUCCESS
    assert sequence(fleet).exit_code == ExitCode.SUCCESS

    cycles = cycles_through_the_projection(fleet)
    assert cycles, "a planted 2-repo cycle must reach MigrationState.cycles"
    finding = cycles[0]
    assert sorted(finding.members) == ["acme-app-ts", "acme-lib-ts"], finding
    assert finding.break_strategy is BreakStrategy.EDGE_BREAK, finding
    assert finding.broken_edge_keys, "a cycle is broken, not merely detected (§3.1 step 6)"

    # Re-sequencing re-derives rather than accumulating a second opinion (§11.7).
    assert sequence(fleet).exit_code == ExitCode.SUCCESS
    assert len(cycles_through_the_projection(fleet)) == len(cycles)


def test_the_run_digest_moves_when_a_cycle_break_decision_moves_and_not_otherwise(
    fleet: Path,  # noqa: F811
) -> None:
    """§11.6 claimed `CycleFinding.break_strategy` as a digest input while never receiving one.

    That is the quiet failure this pair of assertions closes. `run_digest` is the §12.21
    run-equivalence proof: two runs are equivalent **iff** their digests match. Its `cycles`
    section hashed an empty list, so a run that broke an SCC by suppressing an edge and a run that
    migrated the same SCC as one atomic wave — genuinely different plans, different PRs, different
    review surface — produced the same `cycles` digest and were certified equivalent.

    Both directions are asserted, because either alone is worthless: a digest that always changed
    would also "detect" this, and a digest that never changed is what we had.
    """
    plant_cycle(fleet)
    assert scan(fleet).exit_code == ExitCode.SUCCESS
    assert sequence(fleet).exit_code == ExitCode.SUCCESS
    first = digest_of(fleet)
    assert first["sections"]["cycles"] != EMPTY_SECTION_DIGEST, (
        "the `cycles` section must actually carry a finding, or both halves of this test pass "
        "vacuously over the empty list §11.6 used to hash"
    )

    # Nothing relevant changed: the same decision re-derived is the same digest, byte for byte.
    assert sequence(fleet).exit_code == ExitCode.SUCCESS
    assert digest_of(fleet) == first

    # The decision changes — the SCC is now migrated as one atomic wave instead of edge-broken.
    assert sequence(fleet, "--scc-atomic-threshold", "1").exit_code == ExitCode.SUCCESS
    changed = digest_of(fleet)
    assert changed["sections"]["cycles"] != first["sections"]["cycles"], changed
    assert changed["digest"] != first["digest"], changed
    assert cycles_through_the_projection(fleet)[0].break_strategy is BreakStrategy.ATOMIC_WAVE


# ---------------------------------------------------------------------------------------
# 7. the flags that parse but cannot be honoured are refused, not ignored
# ---------------------------------------------------------------------------------------


def test_pr_refuses_the_flags_it_cannot_honour(
    fleet: Path, monorepo: Path, bazel: FakeBazel, forge: FakeForge  # noqa: F811
) -> None:
    """§10: a flag either does what it says or is an exit-2 refusal naming why.

    `--no-draft` would override a verdict — `_must_be_draft` derives the draft from the
    `VerificationReport`, and a stub-limited or closure-sampled PR opened ready-for-review puts
    §3.4's disclosure after the reviewer. `--push` names a push no module in `src/fleet/vcs/`
    performs, which would open a PR against a head ref the forge has never seen. Accepting either
    silently is the failure this test exists to make impossible.
    """
    verified(fleet)
    for flag in ("--no-draft", "--push"):
        result = runner.invoke(app, [*base_args(fleet), "pr", flag], catch_exceptions=False)
        assert result.exit_code == ExitCode.USAGE, (flag, result.output)
    assert not forge.calls, "a refused flag must reach no subprocess at all"


# ---------------------------------------------------------------------------------------
# 8. the forge is chosen by CONFIG — the same verb, against a self-hosted Gitea
# ---------------------------------------------------------------------------------------
# `vcs/gitea.py` is proven against the operator's live Gitea in `tests/test_gitea.py`. What is
# unproven there, and is what these tests are for, is the WIRING: that `pr.forge` reaches
# `build_forge` through `PrwriterInput` and `_pr_sync_impl`, that the CLI's error text names the
# configured forge, and — the one that cannot be allowed to regress — that the credential stays
# in its file and never reaches an argv the harness persists.

#: Never a real credential. Shaped like a Gitea token so an argv/DB scan has something with the
#: right silhouette to find, and written into the `curl -K` file exactly as the real one is.
DECOY_TOKEN = "9f8e7d6c5b4a39281706f5e4d3c2b1a099887766"  # noqa: S105 - a decoy, valid nowhere

GITEA_URL = "http://gitea.invalid:3001"
GITEA_OWNER = "redmage"
GITEA_REPO = "acme-monorepo"


def use_gitea(root: Path) -> Path:
    """Point `config/fleet.yaml` at a Gitea, and write the mode-600 `curl -K` file it names.

    The token goes into the FILE and nowhere else — no env var, no config value, no argv. That is
    the whole shape of §11.4's answer here, so the fixture has to build it that way or the test
    that scans for the token would be scanning for something the harness never had.
    """
    secrets = root / ".secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    config_file = secrets / "gitea-curl.conf"
    config_file.write_text(f'header = "Authorization: token {DECOY_TOKEN}"\n', encoding="utf-8")
    config_file.chmod(0o600)

    fleet_yaml = root / "config" / "fleet.yaml"
    fleet_yaml.write_text(
        fleet_yaml.read_text(encoding="utf-8")
        + "pr:\n"
        + "  forge: gitea\n"
        + f"  forge_url: {GITEA_URL}\n"
        + f"  forge_owner: {GITEA_OWNER}\n"
        + f"  forge_repo: {GITEA_REPO}\n"
        + "  forge_token_config: .secrets/gitea-curl.conf\n",
        encoding="utf-8",
    )
    return config_file


class FakeGitea:
    """`curl` against a Gitea API, recorded and answered from a table the test owns.

    Deliberately a *curl* fake and not a `GiteaForge` fake: the thing under test is the command
    the harness builds, so the seam has to sit where the process would. Everything above it is
    shipped code — `GiteaForge`'s argv construction, its `-w '\\n%{http_code}'` status parsing,
    `parse_pr_json` (which reads `merged` BEFORE `state`, because a merged Gitea PR reports
    `state: "closed"`), and every state transition downstream.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.state: dict[str, str] = {}
        self.titles: dict[str, str] = {}
        self.heads: dict[str, str] = {}
        self._created: dict[str, tuple[str, ...]] = {}
        self._next_index = 1

    # -- the forge side, driven by the test --------------------------------------------

    def url_for(self, repo_id: str) -> str:
        return next(url for url, head in self.heads.items() if head == f"migrate/{repo_id}")

    def merge(self, *repo_ids: str) -> None:
        """A human merged these PRs. The harness is told nothing; it must poll to find out."""
        for repo_id in repo_ids:
            self.state[self.url_for(repo_id)] = "merged"

    def created(self) -> dict[str, tuple[str, ...]]:
        """`head branch -> argv` for every create call."""
        return dict(self._created)

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
        _ = (env, deadline, timeout_s, cwd)
        call = tuple(argv)
        self.calls.append(call)
        method = call[call.index("-X") + 1] if "-X" in call else "GET"
        url = call[-1]
        body = _request_body(call)
        return self._reply(call, self._route(method, url, body, call))

    def _route(
        self, method: str, url: str, body: dict[str, Any], call: tuple[str, ...]
    ) -> dict[str, Any]:
        if url.endswith("/api/v1/user"):
            return {"login": GITEA_OWNER}
        if method == "POST" and url.endswith("/pulls"):
            return self._create(body, call)
        if method == "PATCH":
            self.titles[_pr_path(url)] = str(body["title"])
            return self._view(_pr_path(url))
        return self._view(_pr_path(url))

    def _create(self, body: dict[str, Any], call: tuple[str, ...]) -> dict[str, Any]:
        index = self._next_index
        self._next_index += 1
        html_url = f"{GITEA_URL}/{GITEA_OWNER}/{GITEA_REPO}/pulls/{index}"
        self.state[html_url] = "open"
        self.titles[html_url] = str(body["title"])
        self.heads[html_url] = str(body["head"])
        self._created[str(body["head"])] = call
        return {"html_url": html_url, "number": index}

    def _view(self, url: str) -> dict[str, Any]:
        state = self.state.get(url, "open")
        merged = state == "merged"
        return {
            # Gitea reports a MERGED pr as `state: "closed"`. Faithfully reproduced, because a
            # driver that read `state` alone would call every landed dependency CLOSED.
            "state": "closed" if merged else state,
            "merged": merged,
            "merged_at": "2026-08-09T12:00:00Z" if merged else None,
            "merge_commit_sha": "e" * 40 if merged else None,
            "title": self.titles.get(url, ""),
        }

    def _reply(self, call: tuple[str, ...], body: dict[str, Any]) -> ProcResult:
        return ProcResult(
            argv=call,
            exit_code=0,
            stdout_tail=f"{json.dumps(body)}\n200",
            stderr_tail="",
            duration_ms=3,
            timed_out=False,
        )


def _request_body(call: tuple[str, ...]) -> dict[str, Any]:
    """Read back the `--data-binary @file` payload. Sync, outside the async body (ruff ASYNC240) —
    and reading it from the FILE is the assertion: a request body never travels as argv (§11.4)."""
    if "--data-binary" not in call:
        return {}
    path = Path(call[call.index("--data-binary") + 1].removeprefix("@"))
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _pr_path(url: str) -> str:
    """`…/api/v1/repos/{owner}/{repo}/pulls/{n}` → the html url the driver returned for it."""
    index = url.rsplit("/", 1)[-1]
    return f"{GITEA_URL}/{GITEA_OWNER}/{GITEA_REPO}/pulls/{index}"


@pytest.fixture
def gitea(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGitea]:
    """The `curl` seam, installed for the life of one test and `None` again afterwards."""
    fake = FakeGitea()
    monkeypatch.setattr(cli, "GH_RUNNER", fake)
    yield fake


def test_pr_runs_against_the_forge_config_names_and_never_touches_gh(
    fleet: Path, monorepo: Path, bazel: FakeBazel, gitea: FakeGitea  # noqa: F811
) -> None:
    """`fleet pr` with `pr.forge: gitea` drives the Gitea driver end to end — create, sync, ship.

    WHY: `gh` speaks the GitHub API and nothing else, so an operator on a self-hosted Gitea had no
    PR path at all. `vcs/gitea.py` existing does not give them one; the verb reaching it does. The
    assertion is therefore the whole §3.4 loop through the CLI and not a constructor check: wave 0
    opens, wave 1 is HELD on an unmerged dependency, a human merges, `--sync` ingests `MERGED`
    (which on Gitea is a `state: "closed"` PR with `merged: true`), and the held wave ships.

    `curl` and not `gh` in every argv is the mechanical half: a wiring that fell back to the
    default driver would still open PRs, still return 0, and still be pointed at the wrong host.
    """
    use_gitea(fleet)
    verified(fleet)

    opened = run_pr(fleet)
    assert opened.exit_code == ExitCode.SUCCESS, opened.output
    assert sorted(payload(opened)["opened"]) == list(LIBRARIES), payload(opened)
    assert gitea.calls and all(call[0] == "curl" for call in gitea.calls), gitea.calls

    create = gitea.created()["migrate/acme-lib-py"]
    assert create[create.index("-X") + 1] == "POST", create
    assert create[-1] == f"{GITEA_URL}/api/v1/repos/{GITEA_OWNER}/{GITEA_REPO}/pulls", create

    # Wave 1 is held until the forge — not the harness — says the dependency merged.
    held = run_pr(fleet)
    assert payload(held)["held"] == {
        "acme-app-py": ["acme-lib-py"],
        "acme-app-ts": ["acme-lib-ts"],
    }, payload(held)

    gitea.merge("acme-lib-py")
    synced = run_pr(fleet, "--sync")
    assert synced.exit_code == ExitCode.SUCCESS, synced.output
    assert payload(synced)["merged"] == ["acme-lib-py"], payload(synced)
    assert pr_states(fleet)["acme-lib-py"] == PrState.MERGED.value, pr_states(fleet)

    shipped = run_pr(fleet)
    assert list(payload(shipped)["opened"]) == ["acme-app-py"], payload(shipped)


def test_the_gitea_token_reaches_no_argv_the_harness_persists(
    fleet: Path, monorepo: Path, bazel: FakeBazel, gitea: FakeGitea  # noqa: F811
) -> None:
    """The credential lives in a mode-600 file and in nothing else. Non-negotiable (§11.4).

    WHY: `attempts.command` persists argv verbatim and `WorkerError.stderr_tail` can quote a
    command line, so `-H "Authorization: token …"` would write a live credential into `fleet.db` in
    cleartext — on a host that already carries 251 mirror remotes with embedded PATs. The whole
    reason `pr.forge_token_config` is a PATH is to make that structurally impossible, so the test
    is a search for the token's bytes rather than an inspection of one code path: it scans every
    argv the harness executed, every `attempts.command` row a full run wrote, and finally the raw
    database file — which also covers `findings`, `events` and any column added later.

    The control is `_the_config_path_is_what_travels`: the token IS in the file the argv names, so
    a fake that never wrote one would make every assertion below vacuous.
    """
    config_file = use_gitea(fleet)
    assert DECOY_TOKEN in config_file.read_text(encoding="utf-8"), "the control: it is in the file"
    verified(fleet)
    assert run_pr(fleet).exit_code == ExitCode.SUCCESS
    gitea.merge("acme-lib-py")
    assert run_pr(fleet, "--sync").exit_code == ExitCode.SUCCESS
    assert run_pr(fleet).exit_code == ExitCode.SUCCESS

    assert gitea.calls, "no forge call was made, so nothing below proves anything"
    for call in gitea.calls:
        assert not any(DECOY_TOKEN in part for part in call), call
        assert "-K" in call and str(config_file) in call, call

    rows = query(fleet, "SELECT command FROM attempts WHERE command IS NOT NULL")
    assert rows, "no attempts rows were written, so this scan proves nothing"
    for (command,) in rows:
        assert DECOY_TOKEN not in str(command), command

    assert DECOY_TOKEN.encode() not in (fleet / "state" / "fleet.db").read_bytes()


def test_a_forge_failure_names_the_configured_forge_and_not_gh(
    fleet: Path, monorepo: Path, bazel: FakeBazel, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """A failure message must send the operator to the host they configured.

    WHY: an operator running `pr.forge: gitea` has no `gh` on the box at all. "`gh` failed for 2
    repo(s)" sends them to install and authenticate a binary that has nothing to do with the
    failure, while the Gitea their PRs were actually refused by goes uninvestigated. The forge is
    config, so the diagnosis has to be too.
    """
    use_gitea(fleet)

    async def refuse(
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        deadline: float | None = None,
        timeout_s: float | None = None,
    ) -> ProcResult:
        _ = (cwd, env, deadline, timeout_s)
        return ProcResult(
            argv=tuple(argv),
            exit_code=0,
            stdout_tail='{"message":"token does not have permission"}\n403',
            stderr_tail="",
            duration_ms=1,
            timed_out=False,
        )

    monkeypatch.setattr(cli, "GH_RUNNER", refuse)
    verified(fleet)
    result = runner.invoke(app, [*base_args(fleet), "pr"], catch_exceptions=False)
    assert result.exit_code == ExitCode.UNEXPECTED_ERROR, result.output
    assert "gitea" in result.output, result.output
    assert "`gh` failed" not in result.output, result.output


# ---------------------------------------------------------------------------------------
# The LLM findings sink is drained by `fleet pr` (§13 row 37)
# ---------------------------------------------------------------------------------------


class DriftingBackend:
    """A registered backend that promises the top structured-output rung and honours none of it.

    The shape of a local server that advertises guided JSON in `models.yaml` and silently ignores
    the parameter. What it *returns* is irrelevant to this test: `_emit_drift` fires once per
    TARGET before the call, whether or not the call then succeeds (§13 row 37), so the reply here
    is deliberately unusable and `prwriter` falls back to its non-prose body exactly as it does
    today with an empty registry.
    """

    name = "anthropic"
    version = 1

    def declared_capabilities(self, target: Any) -> Any:
        from fleet.models.tasks import ModelCapabilities

        return ModelCapabilities(
            supports_json_schema=False,
            supports_tools=False,
            supports_constrained_decoding=False,
            max_output_tokens=4096,
            structured_output_modes=(
                StructuredOutputMode.JSON_SCHEMA,
                StructuredOutputMode.PROMPTED,
            ),
        )

    async def invoke(
        self, target: Any, messages: Any, schema: Any, mode: Any, **kwargs: Any
    ) -> Any:
        raise TransportError("the fixture endpoint answers nothing", trigger="CONNECTION")


def test_fleet_pr_persists_the_llm_findings_its_own_run_computed(
    fleet: Path,  # noqa: F811
    monorepo: Path,  # noqa: F811
    bazel: FakeBazel,  # noqa: F811
    forge: FakeForge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`fleet pr` builds a full `RunContext` and drives `PrwriterWorker` **directly**, with no
    `PhaseRunner` anywhere in `_emit_prs`. Every `RunContext` gets an `LlmFindingSink` wired into
    its client, and `PhaseRunner` is what drains one — so before this fix the command computed
    `CapabilityDrift` records for every PR in the fleet and discarded all of them when the
    `StateWriter` closed.

    That is this lane's own defect — "computed, then discarded" — living in a shipped command, and
    it survived the first review because `grep model_client src/fleet/cli.py` is empty: the PR
    path reaches the client through `WorkerContext.llm`, not through that name. Importer identity
    is not call path.

    The assertion is a row in `findings`, read out of the database after the process finished the
    command — the same standard as the rest of the lane. `pr_title` routes to CHEAP and `pr_body`
    to WORKHORSE (`config/models.yaml`), so a drained run leaves one row per tier, which is also
    what shows the drift is attributed to the target rather than to the repo.
    """
    verified(fleet)
    monkeypatch.setattr(client_module, "registry", lambda: {"anthropic": DriftingBackend()})

    result = run_pr(fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert sorted(payload(result)["opened"]) == list(LIBRARIES), (
        "the PRs must still open — a model that cannot answer is a degraded body, not a failure"
    )

    rows = query(fleet, "SELECT repo_id, severity, payload FROM findings WHERE kind = ?",
                 ("CapabilityDrift",))
    assert rows, (
        "`fleet pr` computed the drift and threw it away: no PhaseRunner, therefore no drain"
    )
    assert {r[0] for r in rows} == {None}, (
        "a drift is a property of a TARGET, not of the repo whose PR happened to trigger it"
    )
    payloads = [json.loads(r[2]) for r in rows]
    assert {p["actual"] for p in payloads} == {"PROMPTED"}
    assert {p["promised"] for p in payloads} == {"JSON_SCHEMA"}
    assert {p["tier"] for p in payloads} == {"CHEAP", "WORKHORSE"}, (
        "`pr_title` is CHEAP and `pr_body` is WORKHORSE — both tiers drifted and both were kept"
    )
