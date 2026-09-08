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
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from typer.testing import CliRunner

from fleet import cli
from fleet.cli import ExitCode, app
from fleet.llm import client as client_module
from fleet.llm.client import StructuredOutputMode, TransportError
from fleet.models.enums import BreakStrategy, PrState, RepoStatus, StubFidelity, StubState
from fleet.models.tasks import StubRecord
from fleet.orchestrator.stubs import ProviderFacts, StubTransition, revalidation_key, supersede
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
    write_rules,
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
            case ("pr", "edit", _url, "--body-file", _body_file, *_rest):
                # `Forge.edit_body` (`vcs/github.py:250-255`) — the §12.38/D94 promotion path's
                # body regeneration. Recorded like every other call, so `forge.commands("pr",
                # "edit")` proves it fired without needing a dedicated accessor.
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


def degrade(
    root: Path, repo_id: str, *, state: str = "ACTIVE", provider_repo_id: str = "acme-empty"
) -> None:
    """Put one repo in the §3.5 escape hatch: DEGRADED, with a live `ACTIVE`/`SUPERSEDED` stub row.

    Written straight to SQLite. **Corrected 2026-09-07 (round VI task 69 fix round): this
    docstring used to say "`--stub-blocked` is refused by `fleet build` (no worker emits a stub in
    this tree)" — false since round VI task 69 removed all three `--stub-blocked` refusals and
    wired real stub creation into `_transform_impl`.** The raw-SQL seed stays here regardless, for
    a reason specific to this helper rather than a general absence: most callers pass
    `provider_repo_id='acme-empty'` (see below) — a name deliberately absent from this fixture
    fleet's real repos — which a real `--stub-blocked` dispatch could never produce, since it
    requires a genuine graph edge to a genuine `REQUIRES_HUMAN_INTERVENTION` provider. The one
    caller that DOES want a real provider (`test_pr_sync_fires_t1_and_enqueues_a_revalidate_
    task_for_a_merged_providers_stub`) still uses this helper rather than a real dispatch because
    driving one here would also need a real abandoned provider and a real wave sequence — see
    `tests/test_pr_e2e.py::test_stub_blocked_creation_reaches_degraded_through_the_real_cli_and_
    feeds_t1_for_real` for that full real-CLI proof, built separately because it needs its own
    fixture topology.

    `state` defaults to `ACTIVE` (the original fixture shape) and also accepts `SUPERSEDED` — the
    OTHER member of `_HELD_STATES`/§12.38's refusal set, so a caller can prove the guard holds on
    both open states, not only the one every prior test exercised.

    `provider_repo_id` defaults to `'acme-empty'`, a name that is deliberately NOT one of this
    fixture fleet's four real repos — every original caller of this helper wants a stub whose
    provider never appears in `fleet pr --sync`'s ingested state, so a real merge can never
    resolve it. A T1 test needs the opposite: a provider that IS one of the fixture's real repos,
    so a real `forge.merge()` + `--sync` really drives `orchestrator.stubs.supersede`.
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
            "                   stub_fidelity, resolved_at, state_changed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, '1.2.3', ?, ?, 'PUBLISHED_ARTIFACT', "
            "        ?, ?, ?)",
            (
                # A real UUID hex, not the readable placeholder this literal used to be: nothing
                # ever asserts this value by name, and `StubRecord.stub_id: UUID` (production, not
                # test-only) rejects anything else — `_stub_supersede_inputs` (T1's production
                # trigger) is the first code path this fixture exercises that actually parses it.
                str(uuid4()),
                run_id,
                repo_id,
                STUB_COORD,
                repo_id,
                provider_repo_id,
                f"//third_party/stubs/{STUB_COORD}",
                state,
                # `schema.sql`'s CHECK requires `resolved_at` on any non-ACTIVE row (it is set "on
                # entry to SUPERSEDED/RESOLVED/ABANDONED"). `None` for ACTIVE preserves the
                # original fixture's row exactly.
                None if state == "ACTIVE" else "2026-08-09T00:00:00+00:00",
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


def test_pr_sync_fires_t1_and_enqueues_a_revalidate_task_for_a_merged_providers_stub(
    fleet: Path, monorepo: Path, bazel: FakeBazel, forge: FakeForge  # noqa: F811
) -> None:
    """D101 Half B / D102: `fleet pr --sync` observing a provider's PR go `MERGED` must itself
    fire T1 (`orchestrator.stubs.supersede`) for every stub naming that provider — the trigger had
    ZERO production call sites anywhere in `src/` before this wiring (D102). Proven here rather
    than in `tests/test_stubs.py` because T1's precondition is `RepoStatus.SUCCEEDED` **and**
    `PullRequestDraft.state == 'MERGED'` (ADR-0011 stacking), and only a real `fleet pr --sync`
    against a real forge answer can produce that second half honestly.

    `acme-app-py` is `DEGRADED` with an `ACTIVE` stub whose `provider_repo_id` is `acme-lib-py` —
    unlike every other `degrade()` caller in this file, a REAL fixture repo, not the placeholder
    `'acme-empty'`, so a real `forge.merge()` really supersedes it. `acme-lib-ts` merges in the
    SAME `--sync` invocation with no stub naming it anywhere — the control the brief asks for: a
    merge with no `ACTIVE` stub must leave `stubs`/`tasks` untouched, in the same run that proves
    the positive case, not a separately-argued claim.
    """
    verified(fleet)
    degrade(fleet, "acme-app-py", provider_repo_id="acme-lib-py")
    assert run_pr(fleet).exit_code == ExitCode.SUCCESS

    forge.merge("acme-lib-py", "acme-lib-ts")
    synced = run_pr(fleet, "--sync")
    assert synced.exit_code == ExitCode.SUCCESS, synced.output
    ingested = payload(synced)
    assert sorted(ingested["merged"]) == ["acme-lib-py", "acme-lib-ts"], ingested

    stub_rows = query(
        fleet,
        "SELECT state, revalidation_task_id FROM stubs "
        " WHERE consumer_repo_id = 'acme-app-py' AND stub_coord_key = ?",
        (STUB_COORD,),
    )
    assert [row[0] for row in stub_rows] == ["SUPERSEDED"], stub_rows

    task_rows = query(
        fleet,
        "SELECT task_id, repo_id, revalidation_key, phase, status FROM tasks "
        " WHERE kind = 'REVALIDATE'",
    )
    assert len(task_rows) == 1, (
        f"exactly one REVALIDATE task: acme-app-py's supersede, and NOTHING for acme-lib-ts's "
        f"merge (no stub names it) — got {task_rows}"
    )
    task_id, repo_id, key, task_phase, task_status = task_rows[0]
    assert str(repo_id) == "acme-app-py", task_rows
    assert str(key) == revalidation_key(1, ("acme-lib-py",)), task_rows
    assert int(task_phase) == 4, task_rows  # Phase.VERIFY
    # D102's own scope boundary: nothing executes a REVALIDATE task yet (no worker exists), so
    # the row is expected to sit PENDING forever — that is disclosed, not asserted as a defect.
    assert str(task_status) == "PENDING", task_rows

    # D103 gap 2: `stubs.revalidation_task_id` (SPEC §3.5.1 step 4) names the REVALIDATE task
    # this SAME transaction just minted — a real schema column that was written NOWHERE in
    # production before this task, so a future D101 Half B(ii) can read it instead of
    # re-deriving which task a held consumer is waiting on.
    assert stub_rows[0][1] == str(task_id), (stub_rows, task_id)


def test_pr_sync_sweeps_a_pre_merged_providers_stub_left_active_by_a_prior_crash(
    fleet: Path, monorepo: Path, bazel: FakeBazel, forge: FakeForge  # noqa: F811
) -> None:
    """D103 gap 1: the per-repo loop above fires T1 only for a PR OBSERVED going `MERGED` in the
    SAME `--sync` invocation. `_pr_sync_impl`'s three per-repo transactions (PR record write,
    `pr_merged` emit, T1's own effect) mean a crash between the first and third leaves a durably
    `MERGED` PR record whose stub is still `ACTIVE`, with T1 never having fired for it — and every
    later `--sync` used to leave it that way forever, because `acme-lib-py` is terminal and drops
    out of `pollable` the moment it is first ingested.

    Reproduced without an injected crash: `acme-lib-py` merges and is durably ingested BEFORE any
    stub names it, so T1 correctly no-ops on that first `--sync` (real, not simulated — there is
    genuinely nothing to supersede yet). The `ACTIVE` stub is planted only afterwards, so no
    `--sync` invocation has ever observed "provider MERGED" and "stub ACTIVE" at the same time —
    exactly the state a crash between the event-emit and T1 would leave, reached here by a
    different, equally real route. A second `--sync`, with `acme-lib-py` already terminal and
    nothing new on the forge, must still supersede the stub — that is the sweep, not the loop.
    `acme-lib-ts` never merges and never gets a stub naming it: the control proving the sweep
    fires only on an actually-MERGED, actually-ACTIVE pairing, not on every terminal PR.
    """
    verified(fleet)
    assert run_pr(fleet).exit_code == ExitCode.SUCCESS

    forge.merge("acme-lib-py")
    first_sync = run_pr(fleet, "--sync")
    assert first_sync.exit_code == ExitCode.SUCCESS, first_sync.output
    assert payload(first_sync)["merged"] == ["acme-lib-py"], payload(first_sync)
    # No ACTIVE stub named acme-lib-py yet: T1 genuinely had nothing to do.
    assert query(fleet, "SELECT COUNT(*) FROM tasks WHERE kind = 'REVALIDATE'")[0][0] == 0

    # The crash-window state: a durably-MERGED provider, and only now an ACTIVE stub against it.
    degrade(fleet, "acme-app-py", provider_repo_id="acme-lib-py")

    swept = run_pr(fleet, "--sync")
    assert swept.exit_code == ExitCode.SUCCESS, swept.output
    ingested = payload(swept)
    # acme-lib-py is terminal (MERGED) and out of `pollable`; nothing is newly observed. The
    # sweep, not the per-repo loop, is what has to fire T1 here.
    assert ingested["merged"] == [], ingested
    assert "acme-lib-py" in ingested["terminal"], ingested

    stub_rows = query(
        fleet,
        "SELECT state, revalidation_task_id FROM stubs "
        " WHERE consumer_repo_id = 'acme-app-py' AND stub_coord_key = ?",
        (STUB_COORD,),
    )
    assert [row[0] for row in stub_rows] == ["SUPERSEDED"], stub_rows

    task_rows = query(
        fleet, "SELECT task_id, repo_id, revalidation_key FROM tasks WHERE kind = 'REVALIDATE'"
    )
    assert len(task_rows) == 1, task_rows
    task_id, repo_id, key = task_rows[0]
    assert str(repo_id) == "acme-app-py", task_rows
    assert str(key) == revalidation_key(1, ("acme-lib-py",)), task_rows
    assert stub_rows[0][1] == str(task_id), (stub_rows, task_id)

    # The control: acme-lib-ts never merged, so its own terminal-ness is irrelevant here — it
    # stays OPEN throughout and no stub anywhere names it.
    assert pr_states(fleet)["acme-lib-ts"] == PrState.OPEN.value, pr_states(fleet)
    assert query(fleet, "SELECT COUNT(*) FROM stubs")[0][0] == 1, "only acme-app-py's stub exists"


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


def test_pr_ready_refuses_a_superseded_stub_the_same_as_an_active_one(
    fleet: Path, monorepo: Path, bazel: FakeBazel, forge: FakeForge  # noqa: F811
) -> None:
    """§12.38's refusal set is `{ACTIVE, SUPERSEDED}`, not `ACTIVE` alone (§3.5.1: `SUPERSEDED`
    is a stub whose provider merged but whose revalidation round has not yet PASSed — the label
    was swapped, and the round is still pending). `cli._refuse_unresolved_stubs` names both in one
    `state IN ('ACTIVE','SUPERSEDED')` clause; every other test in this file (and in
    `tests/test_cli.py`) only ever seeds `ACTIVE`, so deleting `'SUPERSEDED'` from that clause
    would still pass the whole suite. This test seeds `SUPERSEDED` alone and asserts the identical
    refusal: exit 2, `gh pr ready` never invoked, state stays `DRAFTED`.
    """
    verified(fleet)
    assert run_pr(fleet).exit_code == ExitCode.SUCCESS
    forge.merge(*LIBRARIES)
    assert run_pr(fleet, "--sync").exit_code == ExitCode.SUCCESS
    degrade(fleet, "acme-app-ts", state="SUPERSEDED")

    result = run_pr(fleet, "--repo", "acme-app-ts")
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert payload(result)["draft"] == ["acme-app-ts"], payload(result)
    assert pr_states(fleet)["acme-app-ts"] == PrState.DRAFTED.value, pr_states(fleet)

    refused = run_pr(fleet, "--ready", "--repo", "acme-app-ts")
    assert refused.exit_code == ExitCode.USAGE, refused.output
    assert "SUPERSEDED" in refused.output, refused.output
    assert not forge.commands("pr", "ready"), forge.calls
    assert pr_states(fleet)["acme-app-ts"] == PrState.DRAFTED.value, pr_states(fleet)


def resolve_stub(root: Path, repo_id: str) -> None:
    """Attach one `RESOLVED` `stubs` row to a repo whose Phase-4 verification is otherwise clean.

    `RESOLVED` is the terminal state a revalidation round leaves once it PASSes against the real
    dependency (`orchestrator.stubs.settle_revalidation`'s T2) — this repo's stub history is real,
    but nothing about it is open any more. Unlike `degrade()`, this does NOT touch `phases.status`
    or `stubbed_deps`: `cli._pr_candidates`'s own stub query is `state IN ('ACTIVE','SUPERSEDED')`
    (`cli.py`, `_pr_candidates`), so a `RESOLVED` row is invisible to `stub_states`/`_must_be_draft`
    by construction, and the repo ships as a plain, non-draft PR.
    """
    run_id = run_id_of(root)
    conn = sqlite3.connect(root / "state" / "fleet.db")
    try:
        conn.execute(
            "INSERT INTO stubs (stub_id, run_id, repo_id, stub_coord_key, consumer_repo_id, "
            "                   provider_repo_id, pinned_version, bazel_label, state, "
            "                   stub_fidelity, revalidation_round, resolved_at, "
            "                   state_changed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'acme-empty', '1.2.3', ?, 'RESOLVED', 'PUBLISHED_ARTIFACT', "
            "        1, ?, ?, ?)",
            (
                "stub-settled",
                run_id,
                repo_id,
                STUB_COORD,
                repo_id,
                f"//third_party/stubs/{STUB_COORD}",
                "2026-08-09T00:00:00+00:00",
                "2026-08-09T00:00:00+00:00",
                "2026-08-09T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_pr_ready_succeeds_once_a_stub_is_genuinely_resolved(
    fleet: Path, monorepo: Path, bazel: FakeBazel, forge: FakeForge  # noqa: F811
) -> None:
    """§12.38's positive half: `fleet pr --ready` succeeds — not merely "does not refuse" — once
    every `stubs` row for the repo is `RESOLVED`. The refusal test above (and every prior test in
    this file) only ever asserts the negative; the guard's positive half — readiness correctly
    GRANTED — was asserted nowhere (round-Z audit, CRITERIA_PLAN.md §38).

    `acme-lib-py` is a graph source (wave 0, no dependencies, so nothing can HOLD it) with a real
    `RESOLVED` stub row attached. `_refuse_unresolved_stubs` does not fire (its query never
    matches `RESOLVED`), `_must_be_draft` does not force a draft (`RESOLVED` is not in
    `prwriter._HELD_STATES` and is filtered out of `stub_states` entirely by `_pr_candidates`'s own
    `ACTIVE`/`SUPERSEDED` query), so the PR opens WITHOUT `--draft` and `PrwriterWorker` issues the
    `READY_UNIT` — `gh pr ready` — in the same invocation, landing `PrState.OPEN` rather than
    `DRAFTED`.
    """
    verified(fleet)
    resolve_stub(fleet, "acme-lib-py")

    result = run_pr(fleet, "--ready", "--repo", "acme-lib-py")
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert payload(result)["opened"] == {"acme-lib-py": forge.url_for("acme-lib-py")}, payload(
        result
    )
    assert payload(result)["draft"] == [], payload(result)

    argv = forge.created()["migrate/acme-lib-py"]
    assert "--draft" not in argv, argv
    assert forge.commands("pr", "ready"), (
        "the READY_UNIT must fire: a genuinely resolved stub is the positive half of §12.38"
    )
    assert pr_states(fleet)["acme-lib-py"] == PrState.OPEN.value, pr_states(fleet)


# ---------------------------------------------------------------------------------------
# 3b. §12.38/D94 — promoting an already-open, HELD PR once its blocking stub resolves
# ---------------------------------------------------------------------------------------


def hold_pr(root: Path, repo_id: str) -> None:
    """Force the exact end state a real end-of-run `stub_reconcile` leaves behind for a consumer
    whose stub it just abandoned: the persisted `PullRequestDraft` moved `DRAFTED` -> `HELD`
    (`cli._apply_stub_reconcile`'s own write). Written straight to SQLite for the same reason
    `degrade`/`resolve_stub` above are: no worker in this tree ever runs a real `fleet resume`
    stub-abandon-then-re-emit-and-resolve cycle within one test, so the PRECONDITION is planted
    directly and the mechanism THIS task built is what gets exercised on top of it.
    """
    run_id = run_id_of(root)
    conn = sqlite3.connect(root / "state" / "fleet.db")
    try:
        row = conn.execute(
            "SELECT payload FROM findings "
            "WHERE run_id = ? AND repo_id = ? AND kind = 'PullRequest'",
            (run_id, repo_id),
        ).fetchone()
        assert row is not None, f"{repo_id} has no PullRequestDraft to hold"
        held = json.loads(str(row[0]))
        held["state"] = "HELD"
        conn.execute(
            "UPDATE findings SET payload = ? "
            " WHERE run_id = ? AND repo_id = ? AND kind = 'PullRequest'",
            (json.dumps(held), run_id, repo_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_pr_attempts_promotion_of_an_already_open_held_pr_once_its_stub_resolves(
    fleet: Path, monorepo: Path, bazel: FakeBazel, forge: FakeForge  # noqa: F811
) -> None:
    """§12.38/D94's TRIGGER, exercised through `fleet pr` itself, not through the private
    functions directly (those are `tests/test_cli.py`'s job).

    `acme-lib-py` (a wave-0 graph source, so nothing else can hold it) is degraded against a live
    stub, opened as a draft, forced `HELD` (the state a real end-of-run `stub_reconcile` leaves
    behind — `hold_pr` above), and the stub is then resolved. A second `fleet pr` invocation must
    NOT treat this as a plain no-op `already_open`: it must ATTEMPT promotion.

    D115 (`filter_repo.py::ingest()`, closed round VI task 50 per ADR-0118) makes §3.3's ingest
    create a local `migrate/acme-lib-py` branch in this monorepo checkout, so `_promote_one_pr`'s
    FIRST precondition check (`record.branch` must exist) now passes. This checkout still has no
    real `origin` remote configured (a separate, disclosed, and still-refused gap — `cli.py:10096
    -10113`'s `--push` refusal), so the attempt now fails at `_promote_one_pr`'s SECOND check
    instead, reading the remote tip of `migrate/acme-lib-py`. That failure is still exactly the
    discriminating proof the trigger fired: a genuinely-just-`already_open` PR (`test_re_running_
    fleet_pr_opens_no_second_pr` above) is never attempted at all and never appears in `failed`.
    """
    verified(fleet)
    degrade(fleet, "acme-lib-py", state="ACTIVE")
    opened = run_pr(fleet, "--repo", "acme-lib-py")
    assert opened.exit_code == ExitCode.SUCCESS, opened.output
    assert pr_states(fleet)["acme-lib-py"] == PrState.DRAFTED.value, pr_states(fleet)

    hold_pr(fleet, "acme-lib-py")
    conn = sqlite3.connect(fleet / "state" / "fleet.db")
    conn.execute(
        "UPDATE stubs SET state = 'RESOLVED', resolved_at = '2026-08-09T00:00:00+00:00' "
        " WHERE run_id = ? AND repo_id = ? AND state = 'ACTIVE'",
        (run_id_of(fleet), "acme-lib-py"),
    )
    conn.commit()
    conn.close()
    assert pr_states(fleet)["acme-lib-py"] == PrState.HELD.value, "setup check: the PR is HELD"

    again = run_pr(fleet, "--repo", "acme-lib-py")
    assert again.exit_code == ExitCode.UNEXPECTED_ERROR, again.output
    body = payload(again)
    assert "acme-lib-py" in body["failed"], body
    assert "could not read the remote tip of" in body["failed"]["acme-lib-py"], body["failed"]
    assert "does not exist" not in body["failed"]["acme-lib-py"], (
        "D115 is fixed: migrate/acme-lib-py must exist locally by the time this runs, so the "
        "FIRST precondition check must not be the one that fires", body["failed"]
    )
    assert body["already_open"] == ["acme-lib-py"], body
    assert body["promoted"] == {}, body
    assert not forge.commands("pr", "ready"), (
        "mark_ready must never fire for a promotion attempt that failed before it got there"
    )
    assert pr_states(fleet)["acme-lib-py"] == PrState.HELD.value, (
        "a failed promotion attempt must leave the PR exactly as it was — Rule 11: reported, "
        "never a silent skip and never a state change on a path that did not succeed"
    )


def test_re_running_fleet_pr_leaves_a_genuinely_already_open_pr_out_of_failed(
    fleet: Path, monorepo: Path, bazel: FakeBazel, forge: FakeForge  # noqa: F811
) -> None:
    """The control for the promotion test above: a plain re-run over PRs that were never `HELD`
    must not even ATTEMPT promotion, so `failed` stays empty and the exit code stays SUCCESS —
    the D94 trigger is scoped to `HELD` and nothing else moves it.
    """
    verified(fleet)
    assert run_pr(fleet).exit_code == ExitCode.SUCCESS

    again = run_pr(fleet)
    assert again.exit_code == ExitCode.SUCCESS, again.output
    body = payload(again)
    assert body["failed"] == {}, body
    assert body["promoted"] == {}, body
    assert sorted(body["already_open"]) == list(LIBRARIES), body


def _git_rev(repo: Path, ref: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "rev-parse", ref],  # noqa: S607 - `git` from PATH, as elsewhere
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return (
        subprocess.run(  # noqa: S603
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],  # noqa: S607
            check=False,
        ).returncode
        == 0
    )


def promotion_remote_for(monorepo_path: Path) -> Path:
    """Give the e2e monorepo checkout (`tests/test_build_e2e.py::make_monorepo`, `git init`-only,
    no `origin`) a real bare remote — the shape `_promote_one_pr`'s remote-tip read
    (`cli.py::_remote_branch_tip`) needs. Mirrors `tests/test_cli.py::_promotion_repo`'s
    bare-remote pattern, but built around the REAL monorepo this suite's own pipeline ingests
    into, not a throwaway one.

    Call this AFTER `verified()` has run (so `migrate/<repo>` branches D115 creates already exist
    locally) and after any commit meant to simulate work landing on `integration` while a PR was
    `HELD`, so the push captures both branches at the tips the promotion attempt should see.
    """
    remote = monorepo_path.parent / "monorepo-remote.git"
    _git(monorepo_path.parent, "init", "-q", "--bare", str(remote))
    _git(monorepo_path, "remote", "add", "origin", str(remote))
    _git(monorepo_path, "push", "-q", "origin", "integration", "migrate/acme-lib-py")
    return remote


def pr_record(root: Path, repo_id: str) -> dict[str, Any]:
    """The full persisted `PullRequestDraft` payload for one repo. `pr_states()` above only
    projects `state` — some assertions (`url`, `revalidation_round`) need the whole record."""
    rows = query(
        root,
        "SELECT payload FROM findings WHERE kind = 'PullRequest' AND repo_id = ? "
        "ORDER BY repo_id",
        (repo_id,),
    )
    assert rows, f"no PullRequestDraft for {repo_id}"
    return dict(json.loads(str(rows[0][0])))


def test_pr_promotes_an_already_open_held_pr_through_the_real_cli_end_to_end(
    fleet: Path, monorepo: Path, bazel: FakeBazel, forge: FakeForge  # noqa: F811
) -> None:
    """§12.38/D94's resolution mechanics (clauses 6-12), driven through the REAL `fleet pr` CLI
    end to end — the gap research-31 found: the trigger test above proves the promotion is
    ATTEMPTED but deliberately stops at `_promote_one_pr`'s second precondition (no real `origin`
    in that fixture); `tests/test_cli.py::test_promote_one_pr_rebases_regenerates_the_body_and_
    marks_ready` (+ siblings) proves the git/forge mechanics but calls `_promote_one_pr` directly,
    bypassing `_pr_impl`/`_promote_prs`. This test connects the two: a real `origin` remote, a
    real `HELD`-then-resolved PR, and a plain `fleet pr --repo acme-lib-py` re-run that must
    actually SUCCEED and write the promoted state.

    Same setup as `test_pr_attempts_promotion_of_an_already_open_held_pr_once_its_stub_resolves`
    (degrade -> open -> `hold_pr` -> resolve the stub), reusing its helpers rather than
    duplicating them, but against a monorepo checkout that has a real bare `origin` — with an
    extra commit landed on `integration` first, standing in for "someone else's PR merged while
    ours was HELD" (mirrors `tests/test_cli.py::_promotion_repo`'s `b.txt` commit), so the
    post-promotion ancestor check proves a genuine rebase and not a no-op.
    """
    verified(fleet)
    degrade(fleet, "acme-lib-py", state="ACTIVE")
    opened = run_pr(fleet, "--repo", "acme-lib-py")
    assert opened.exit_code == ExitCode.SUCCESS, opened.output
    assert pr_states(fleet)["acme-lib-py"] == PrState.DRAFTED.value, pr_states(fleet)
    pre_promotion_url = pr_record(fleet, "acme-lib-py")["url"]

    hold_pr(fleet, "acme-lib-py")
    conn = sqlite3.connect(fleet / "state" / "fleet.db")
    conn.execute(
        "UPDATE stubs SET state = 'RESOLVED', resolved_at = '2026-08-09T00:00:00+00:00' "
        " WHERE run_id = ? AND repo_id = ? AND state = 'ACTIVE'",
        (run_id_of(fleet), "acme-lib-py"),
    )
    conn.commit()
    conn.close()
    assert pr_states(fleet)["acme-lib-py"] == PrState.HELD.value, "setup check: the PR is HELD"

    # "Someone else landed while the PR was HELD" — a real commit on `integration`, so the
    # promotion's rebase has genuine work to do and is not a trivial no-op fast-forward.
    (monorepo / "someone-else-landed.txt").write_text("late arrival\n", encoding="utf-8")
    _git(monorepo, "add", "-A")
    _git(monorepo, "commit", "-q", "-m", "someone else landed while the PR was HELD")

    remote = promotion_remote_for(monorepo)

    again = run_pr(fleet, "--repo", "acme-lib-py")
    assert again.exit_code == ExitCode.SUCCESS, again.output
    body = payload(again)
    assert body["promoted"] == {"acme-lib-py": forge.url_for("acme-lib-py")}, body
    assert body["failed"] == {}, body

    assert pr_states(fleet)["acme-lib-py"] == PrState.OPEN.value, pr_states(fleet)
    record = pr_record(fleet, "acme-lib-py")
    assert record["url"] == pre_promotion_url, record
    assert record["revalidation_round"] == 1, record

    assert forge.commands("pr", "ready"), (
        "clause 9/10/11: mark_ready must fire once rebase+push+body-edit all succeeded"
    )
    assert forge.commands("pr", "edit"), "clause 11: the body must be regenerated on the forge"
    creates_for_acme_lib_py = [
        call for call in forge.commands("pr", "create") if "migrate/acme-lib-py" in call
    ]
    assert len(creates_for_acme_lib_py) == 1, (
        "clause 12 (positive half): no second `gh pr create` for this repo", forge.calls
    )
    assert not forge.commands("pr", "close"), (
        "clause 12 (negative half): a resolution never closes and re-opens a PR", forge.calls
    )

    integration_tip = _git_rev(remote, "integration")
    branch_tip = _git_rev(remote, "refs/heads/migrate/acme-lib-py")
    assert _is_ancestor(remote, integration_tip, branch_tip), (
        "the remote migrate/acme-lib-py branch must have been rebased onto integration's "
        "current tip, not merely force-pushed unchanged"
    )


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
# 6b. §12.19's remaining leg — `PullRequestDraft.scc_id` shared by every ATOMIC_WAVE member,
#     not just `CycleFinding.scc_id`, and the intra-SCC deadlock hazard closed alongside it
# ---------------------------------------------------------------------------------------


def pr_records_through_the_module(root: Path) -> dict[str, Any]:
    """`cli._pr_records`, via the real reader -- never by re-parsing `findings` by hand here."""

    async def read() -> dict[str, Any]:
        conn = await connect_ro(root / "state" / "fleet.db")
        try:
            return await cli._pr_records(conn, run_id_of(root))
        finally:
            await conn.close()

    return asyncio.run(read())


def verified_through_a_planted_cycle(root: Path) -> list[Any]:
    """Plant a real 2-repo cycle, force it ATOMIC_WAVE, and drive it all the way to VERIFY.

    Mirrors `verified()` above but must sequence with `--scc-atomic-threshold 1` BEFORE transform
    runs, since Phase 3 coarsens an ATOMIC_WAVE SCC into one shared Bazel target (§3.1 6e) — a
    plain `scanned()` would resolve this same 2-repo cycle as `EDGE_BREAK` instead.
    """
    plant_cycle(root)
    assert scan(root).exit_code == ExitCode.SUCCESS
    assert sequence(root, "--scc-atomic-threshold", "1").exit_code == ExitCode.SUCCESS
    assert transform(root).exit_code == ExitCode.SUCCESS
    assert build(root, "--no-sandbox").exit_code == ExitCode.SUCCESS
    assert verify(root).exit_code == ExitCode.SUCCESS
    cycles = cycles_through_the_projection(root)
    assert cycles, "the planted cycle must reach MigrationState.cycles"
    assert cycles[0].break_strategy is BreakStrategy.ATOMIC_WAVE, cycles[0]
    return cycles


def test_an_atomic_wave_scc_ships_one_pr_shared_by_every_member(
    fleet: Path, monorepo: Path, bazel: FakeBazel, forge: FakeForge  # noqa: F811
) -> None:
    """§12.19's remaining leg: `PullRequestDraft.scc_id` -- not just `CycleFinding.scc_id` -- is
    shared by every ATOMIC_WAVE member, and exactly ONE `gh pr create` fires for the whole SCC.

    This fixture fleet has FOUR repos, only two of which are in the planted cycle, so the same
    `fleet pr` invocation also ships the untouched `acme-lib-py` (independent, wave 0) with its
    OWN separate PR -- correctly, since it shares no SCC with anything. The assertion below is
    therefore scoped to the two SCC members' own branches, not to the total call count for the
    whole fleet: it proves the SCC fires exactly one `gh pr create` (for the primary member's
    branch) and NEVER a second one for its fellow member, which is what §12.19's remaining leg is
    actually about. `test_pr_opens_one_pr_per_eligible_repo_...` already proves the ordinary
    per-repo case unaffected.
    """
    finding = verified_through_a_planted_cycle(fleet)[0]
    members = sorted(finding.members)
    assert members == ["acme-app-ts", "acme-lib-ts"], finding
    primary = min(members)

    result = run_pr(fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    created = forge.created()  # head branch -> argv, one entry per REAL `gh pr create` call
    member_branches = {f"migrate/{member}" for member in members}
    fired_for_scc = member_branches & set(created)
    assert fired_for_scc == {f"migrate/{primary}"}, (
        "the SCC must fire exactly one `gh pr create`, for the primary member's branch only "
        f"(§12.19's remaining leg) -- got {fired_for_scc}"
    )

    records = pr_records_through_the_module(fleet)
    assert set(members) <= set(records), records  # both SCC members got their own record
    urls = {records[member].url for member in members}
    assert len(urls) == 1, "every member's PullRequestDraft.url must be the SAME url"
    scc_ids = {records[member].scc_id for member in members}
    assert scc_ids == {finding.scc_id}, scc_ids
    member_sets = {tuple(sorted(records[member].member_repo_ids)) for member in members}
    assert member_sets == {tuple(members)}, member_sets

    phase_urls = dict(query(fleet, "SELECT repo_id, pr_url FROM phases WHERE phase = 4"))
    for member in members:
        assert phase_urls[member] in urls, phase_urls


def test_an_atomic_wave_member_is_not_blocked_on_its_own_scc_mates_pr(
    fleet: Path, monorepo: Path, bazel: FakeBazel, forge: FakeForge  # noqa: F811
) -> None:
    """The deadlock hazard `research-11`/task 18's brief found: `ATOMIC_WAVE` does not suppress
    the intra-SCC ordering edge, so `acme-lib-ts` -> `acme-app-ts` (planted by `plant_cycle`) is a
    real, unsuppressed row in `edges`. Without filtering it out of `_pr_candidates`' dependency
    set, each member would show up "blocking" on its own fellow member's PR being MERGED -- a PR
    that cannot be MERGED before it exists, because they are going to share ONE not-yet-opened PR.
    This asserts neither member is HELD, and that the shared PR opens in the same invocation.
    """
    finding = verified_through_a_planted_cycle(fleet)[0]
    members = sorted(finding.members)

    result = run_pr(fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output
    body = payload(result)

    held = cast(dict[str, list[str]], body["held"])
    for member in members:
        assert member not in held, (
            f"{member} is HELD on {held.get(member)} -- an SCC mate's own not-yet-opened shared "
            "PR must never be read as a blocking dependency"
        )
    opened = cast(dict[str, str], body["opened"])
    for member in members:
        assert member in opened, body  # shipped, not just un-held


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


class AnsweringBackend:
    """A registered backend that actually ANSWERS, honestly, at the top rung.

    `DriftingBackend` above cannot exercise §12.18's `llm_call` event: its `invoke` always raises
    `TransportError`, so `LadderModelClient._call_target`'s `backend.invoke()` never RETURNS and
    `_emit_llm_call` never fires — see `LlmCall`'s own docstring on why a raised `TransportError`
    produces no event. This backend declares full JSON_SCHEMA support (so `invoke` receives the
    real schema, unlike PROMPTED mode where it is folded into the prompt text) and answers every
    required string field with a short valid value, which validates against whichever of
    `PrTitle`/`PrBody` the schema names.
    """

    name = "anthropic"
    version = 1

    def declared_capabilities(self, target: Any) -> Any:
        from fleet.models.tasks import ModelCapabilities

        return ModelCapabilities(
            supports_json_schema=True,
            supports_tools=True,
            supports_constrained_decoding=True,
            max_output_tokens=4096,
            structured_output_modes=(StructuredOutputMode.JSON_SCHEMA,),
        )

    async def invoke(
        self, target: Any, messages: Any, schema: Any, mode: Any, **kwargs: Any
    ) -> Any:
        from fleet.llm.client import BackendReply
        from fleet.models.tasks import TokenUsage

        properties = dict((schema or {}).get("properties", {}))
        answer = {name: "ok" for name, spec in properties.items() if spec.get("type") == "string"}
        return BackendReply(
            text=json.dumps(answer),
            usage=TokenUsage(input_tokens=42, output_tokens=7, model_id=target.model_id),
            finish_reason="stop",
        )


class RefusingBackend:
    """A registered backend whose every reply carries `finish_reason='refusal'` — the §12.18
    `llm_call` ERROR leg: the transport itself flags the reply as unusable. `ModelRefused`
    propagates out of `complete()`, and `prwriter._compose`'s `except LlmError` catches it exactly
    as it already catches `DriftingBackend`'s `TransportError`, so the PR still opens with a
    degraded (prose-less) body rather than failing the run.
    """

    name = "anthropic"
    version = 1

    def declared_capabilities(self, target: Any) -> Any:
        from fleet.models.tasks import ModelCapabilities

        return ModelCapabilities(
            supports_json_schema=True,
            supports_tools=True,
            supports_constrained_decoding=True,
            max_output_tokens=4096,
            structured_output_modes=(StructuredOutputMode.JSON_SCHEMA,),
        )

    async def invoke(
        self, target: Any, messages: Any, schema: Any, mode: Any, **kwargs: Any
    ) -> Any:
        from fleet.llm.client import BackendReply
        from fleet.models.tasks import TokenUsage

        return BackendReply(
            text=None,
            usage=TokenUsage(input_tokens=5, output_tokens=0, model_id=target.model_id),
            finish_reason="refusal",
        )


def _jsonl_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_fleet_pr_llm_calls_reach_the_event_stream_with_every_spec_1218_field(
    fleet: Path,  # noqa: F811
    monorepo: Path,  # noqa: F811
    bazel: FakeBazel,  # noqa: F811
    forge: FakeForge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§12.18: "every `llm_call` event carries `role`, `tier`, `backend`, the resolved `model_id`,
    `structured_output_mode`, token counts, `cost_usd`, and `latency_ms`" — asserted against the
    real `logs/events-<run_id>.jsonl` a real `fleet pr` run produces, not against a mock's call
    log (Task 3's brief: "drive the real CLI, then inspect the filesystem").

    Two libraries open in wave 0, each driving `write_pr_body` (WORKHORSE) then `write_pr_title`
    (CHEAP) once, so a clean run with no retries makes exactly 4 raw `backend.invoke()` calls —
    the count is itself the discriminator against an implementation that fires once per
    `complete()` (which would read 2, one per repo's *first* successful role) or once per repo
    (also 2) instead of once per actual provider call (`LlmCall`'s own docstring on why).

    The same event must also reach `events` under the same `event_uid` (ADR-0012's "one event
    pipeline"), exactly like `pr_merged`'s existing dual-sink test — and a clean run must leave
    `logs/errors-<run_id>.jsonl` ABSENT, which is the "iff" half of §12.18's errors-sink clause.
    """
    verified(fleet)
    monkeypatch.setattr(client_module, "registry", lambda: {"anthropic": AnsweringBackend()})

    result = run_pr(fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert sorted(payload(result)["opened"]) == list(LIBRARIES)

    run_id = run_id_of(fleet)
    events_path = fleet / "logs" / f"events-{run_id}.jsonl"
    errors_path = fleet / "logs" / f"errors-{run_id}.jsonl"

    llm_calls = [line for line in _jsonl_lines(events_path) if line["event"] == "llm_call"]
    assert len(llm_calls) == 4, (
        f"expected one llm_call per actual provider call (2 repos x [title, body]): {llm_calls}"
    )
    for line in llm_calls:
        fields = line["payload"]
        assert fields["role"] in ("pr_title", "pr_body")
        assert fields["tier"] in ("CHEAP", "WORKHORSE")
        assert fields["backend"] == "anthropic"
        assert fields["model_id"]
        assert fields["structured_output_mode"] == "JSON_SCHEMA"
        assert fields["input_tokens"] == 42
        assert fields["output_tokens"] == 7
        assert fields["cost_usd"] >= 0.0
        assert isinstance(fields["latency_ms"], int) and fields["latency_ms"] >= 0
        assert line["level"] == "info"

    conn = sqlite3.connect(fleet / "state" / "fleet.db")
    try:
        sql_uids = {
            row[0] for row in conn.execute("SELECT event_uid FROM events WHERE event = 'llm_call'")
        }
    finally:
        conn.close()
    assert sql_uids == {line["event_uid"] for line in llm_calls}, (
        "the JSONL stream and the `events` table must agree on which llm_call events landed"
    )

    assert not errors_path.exists(), (
        "no error-level event occurred this run — §12.18's errors sink must not exist"
    )


def test_fleet_pr_llm_refusal_reaches_the_errors_sink_without_failing_the_run(
    fleet: Path,  # noqa: F811
    monorepo: Path,  # noqa: F811
    bazel: FakeBazel,  # noqa: F811
    forge: FakeForge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of §12.18's "iff": a run that really does meet a recoverable error produces
    `logs/errors-<run_id>.jsonl`, and the line on it is the SAME redacted line as on the main
    stream (a filtered copy, not a route-away — see `EventEmitter.errors_jsonl_path`'s docstring),
    under the SAME `event_uid`.

    `ModelRefused` is exactly the shape CLAUDE.md's Guardrail 6 calls "accidentally reachable, not
    adversarial-only": a local server or a content filter answering with a refusal is ordinary
    operation, not a test harness reaching for a private subclass — so this run must still SUCCEED
    (`prwriter._compose` catches `LlmError` and falls back to a prose-less body), exactly like the
    existing `DriftingBackend` scenario above.
    """
    verified(fleet)
    monkeypatch.setattr(client_module, "registry", lambda: {"anthropic": RefusingBackend()})

    result = run_pr(fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert sorted(payload(result)["opened"]) == list(LIBRARIES), (
        "a refused model is a degraded body, not a failed run — same contract as TransportError"
    )

    run_id = run_id_of(fleet)
    events_path = fleet / "logs" / f"events-{run_id}.jsonl"
    errors_path = fleet / "logs" / f"errors-{run_id}.jsonl"

    error_calls = [line for line in _jsonl_lines(errors_path) if line["event"] == "llm_call"]
    assert error_calls, "every call was refused, so the errors sink must exist and hold them"
    assert all(line["level"] == "error" for line in error_calls)

    main_calls = {
        line["event_uid"]: line for line in _jsonl_lines(events_path) if line["event"] == "llm_call"
    }
    for line in error_calls:
        assert main_calls.get(line["event_uid"]) == line, (
            "the errors sink must carry the SAME line as the main stream, not a divergent one"
        )


def test_fleet_pr_persists_its_llm_findings_even_when_the_command_fails_partway(
    fleet: Path,  # noqa: F811
    monorepo: Path,  # noqa: F811
    bazel: FakeBazel,  # noqa: F811
    forge: FakeForge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N7. A drain placed *after* the candidate loop is discarded by the runs that need it most.

    `_emit_one_pr` and `_write_pr_record` can both raise; on those runs the loop never reaches its
    end, and a post-loop drain would throw away every finding the run had computed — a narrower
    copy of the very defect the drain was added to fix. The drain therefore lives in a `finally`,
    while the `StateWriter` is still open.

    The failure is injected at `_write_pr_record`, i.e. after the worker has already driven the
    model and buffered its drift, which is precisely the window that was unprotected — and on the
    SECOND candidate, so the loop really is interrupted partway rather than on entry. Wave 0 has
    two libraries, so the first PR's record lands and the second raises: the buffer at that moment
    holds drift from both repos, which is the state a post-loop drain discarded.
    """
    verified(fleet)
    monkeypatch.setattr(client_module, "registry", lambda: {"anthropic": DriftingBackend()})

    real_write = cli._write_pr_record
    calls: list[int] = []

    async def explode_on_the_second(*args: Any, **kwargs: Any) -> None:
        calls.append(1)
        if len(calls) < 2:
            await real_write(*args, **kwargs)
            return
        raise RuntimeError("the state writer fell over mid-loop")

    monkeypatch.setattr(cli, "_write_pr_record", explode_on_the_second)

    result = runner.invoke(app, [*base_args(fleet), "--json", "pr"], catch_exceptions=True)
    assert result.exit_code != ExitCode.SUCCESS, "the injected failure must not be swallowed"
    assert len(calls) == 2, "the loop must have got PAST the first candidate to be 'partway'"
    assert query(fleet, "SELECT repo_id FROM phases WHERE phase = 4 AND pr_url IS NOT NULL"), (
        "the first candidate's record really landed before the second one blew up"
    )

    rows = query(fleet, "SELECT payload FROM findings WHERE kind = ?", ("CapabilityDrift",))
    assert rows, (
        "the run computed drift and then crashed, and the drain ran only after the loop — so "
        "every finding went out with the failure"
    )
    assert {json.loads(r[0])["actual"] for r in rows} == {"PROMPTED"}




# ---------------------------------------------------------------------------------------
# §12.37 Leg 3 (round VI task 69) — the real `--stub-blocked` CLI path end to end
# ---------------------------------------------------------------------------------------

_STUB_PROVIDER = "acme-lib-py"
_STUB_CONSUMER = "acme-app-py"
_STUB_COORD_KEY = "pypi::acme-lib-py"

#: A rule that claims `acme-lib-py`'s own module and matches nothing in it — the PROVIDER-side
#: mirror of `tests/test_transform_e2e.py`'s `PY_MISSING_RULE` (which targets the CONSUMER,
#: `acme_app_py/main.py`). Same RULE_MISS shape (§5), retargeted so the PROVIDER is the one that
#: genuinely, mechanically exhausts its ladder and reaches REQUIRES_HUMAN_INTERVENTION.
_PROVIDER_FAILS_RULE = """\
rules:
  - id: py-lib-missing
    description: claims acme-lib-py's module and matches nothing in it
    engine: fixture
    languages: [python]
    applies_to: ["**/acme_lib_py/__init__.py"]
    rule:
      pattern: "class Registry"
    params:
      find: "class RegistryNotPresent"
      replace: "class Registry"
"""


def _seed_blocked(root: Path, *, repo_id: str, blocked_by: list[str]) -> None:
    """Hand-seed one repo's TRANSFORM `phases` row to `BLOCKED`, bypassing
    `SqliteSchedulerStore.append_blocked_by` — the real writer this test could not reach through.

    **Why this is necessary rather than a shortcut for something a real dispatch could do.**
    **Corrected, round VI task 76 fix round 1 (ADR-0127/D123, D126) — the claim this paragraph
    used to make is now only half true.** `_transform_impl`'s wave loop no longer creates each
    wave's `phases` rows lazily: round VI task 76 fixed D123 by pre-seeding every wave's TRANSFORM
    row upfront, before any wave dispatches, mirroring `_build_impl`'s PASS 1. That fix closes the
    SAME-invocation case this paragraph used to describe — driving both `acme-lib-py`'s and
    `acme-app-py`'s waves through ONE `fleet transform` call (no `--wave`) now correctly leaves
    `acme-app-py` `BLOCKED` with `blocked_by == ["acme-lib-py"]`
    (`tests/test_transform_e2e.py::
    test_a_provider_failing_in_an_earlier_wave_blocks_its_later_wave_dependent_in_one_run`).

    THIS test still needs the hand-seed below, though, because it drives `--wave 0` specifically
    (see the real dispatch a few lines down this file): the pre-seed pass is scoped to the CURRENT
    invocation's own `--wave`-scoped domain (`_open_transform_waves` returns exactly `(wave,)`
    when `wave` is not `None`), so a `--wave 0` call's pre-seed pass never touches wave 1's
    members at all — `acme-app-py` has no `phases` row after this test's own `--wave 0` call,
    regardless of what `propagate_blocked` computes for `acme-lib-py`'s abandonment. This
    narrower, still-open residual is tracked as **D126** (`docs/INTEGRATION_HONESTY.md`) — D123's
    own discovery measured it directly, twice, before the fix (once with both waves driven in ONE
    `fleet transform` call, now fixed above; once across two separate `fleet transform --wave N`
    calls, the same shape this test's own single `--wave 0` call exercises, still open as D126):
    `acme-app-py` reached `SUCCEEDED` with `blocked_by == '[]'` in both cases, never `BLOCKED`,
    after a REAL `_PROVIDER_FAILS_RULE`-driven failure of `acme-lib-py`. This is a genuine,
    structural gap in `_transform_impl`'s `--wave`-scoped semantics (BUILD's own
    `_eligible_build_units`/upfront full-domain `upsert_phase` INGEST pass has no `--wave` filter
    to narrow, which is why `tests/test_build_e2e.py`'s "Blocker C" fixture can drive the SAME
    shape through two real `build()` calls with no seed at all) — not introduced by this leg, not
    fixed by it (out of scope; tracked as D126 for a future round, not patched here).

    `acme-lib-py` itself is NOT hand-seeded: it reaches `REQUIRES_HUMAN_INTERVENTION` for real,
    through a real `fleet transform --wave 0` dispatch, in this test — so the evidence `fleet
    resume`'s step 5 (`_demote_to_floors`) reads (real `attempts` rows, a real `RULE_MISS`
    finding) is genuine and step 5 correctly leaves it at RHI rather than re-deriving a fresh
    floor for it (an earlier draft of this test hand-seeded `acme-lib-py`'s status too, with no
    backing evidence, and step 5 — correctly, from its own perspective — demoted it back to a
    fresh `TRANSFORM` floor and it simply succeeded on retry, silently invalidating the whole
    fixture).
    """
    conn = sqlite3.connect(root / "state" / "fleet.db")
    try:
        run_id = str(conn.execute("SELECT run_id FROM runs").fetchone()[0])
        # An INSERT, not an UPDATE: `_STUB_CONSUMER`'s wave (wave 1) never opened in this test —
        # only `--wave 0` ran — so its `phases(TRANSFORM)` row does not exist yet, and a plain
        # UPDATE would silently affect zero rows (an earlier draft of this helper did exactly
        # that: it "succeeded" for the wrong reason, because step 8 then dispatched the consumer
        # as an ORDINARY un-blocked repo and the trigger still fired on the live edge regardless
        # of blocking — proving stub-creation fires on a normal dispatch, not that the unblock
        # path was exercised at all).
        conn.execute(
            "INSERT INTO phases (run_id, repo_id, phase, status, blocked_by, updated_at) "
            "VALUES (?, ?, 2, 'BLOCKED', ?, ?)",
            (run_id, repo_id, json.dumps(sorted(blocked_by)), "2026-08-09T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def test_stub_blocked_creation_reaches_degraded_through_the_real_cli_and_feeds_t1_for_real(
    fleet: Path,  # noqa: F811
    monorepo: Path,  # noqa: F811
    bazel: FakeBazel,  # noqa: F811
    filter_repo: FakeFilterRepo,  # noqa: F811
) -> None:
    """§12.37's own literal scenario (`docs/SPEC.md:7595`), driven for real through
    `--stub-blocked` end to end for the first time (round VI task 69).

    `acme-lib-py` (`_STUB_PROVIDER`) reaches `REQUIRES_HUMAN_INTERVENTION` through a REAL
    `fleet transform --wave 0` dispatch (three real `RULE_MISS` attempts against
    `_PROVIDER_FAILS_RULE`). `acme-app-py` (`_STUB_CONSUMER`) is hand-seeded `BLOCKED` — see
    `_seed_blocked`'s own docstring for the measured, disclosed reason a real dispatch cannot
    reach that state here. Everything from there on is real:

    1. ONE `fleet resume --stub-blocked` drives step 6 (`_unblock_dependents`, real — frees
       `acme-app-py`) and step 8's REAL `_transform_impl(stub_blocked=True)` continuation:
       `acme-app-py` transforms for real, task-67's trigger detection fires for real against the
       real `edges`/`coordinates` rows `scanned()` produced, and the `RUNNING -> DEGRADED`
       correction (this leg) fires for real.
    2. `fleet build --no-sandbox` / `fleet verify` carry `acme-app-py` through for real (real
       `FakeBazel`/`FakeFilterRepo` argv, no further seeding), proving `_eligible_build_units`/
       `_gated_members`'s widening (this leg) actually admits a `DEGRADED` TRANSFORM row into
       BUILD and BUILD's own new `DEGRADED` row into VERIFY.
    3. The REAL `stubs` row this run created is fed to `orchestrator.stubs.supersede` (D80,
       unmodified) exactly as `tests/test_stubs.py::test_t1_fires_on_succeeded_and_merged` feeds
       its own hand-built fixture — proving the CREATION side lands in the shape D80's tests
       already start from, per this task's brief, rather than re-proving reconciliation itself.
    """
    write_rules(fleet, _PROVIDER_FAILS_RULE)
    scanned(fleet)
    first = transform(fleet, "--wave", "0", json_output=False)
    assert first.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION, first.output
    provider_status = dict(
        query(
            fleet,
            "SELECT repo_id, status FROM phases WHERE phase = 2 AND repo_id = ?",
            (_STUB_PROVIDER,),
        )
    )
    assert provider_status[_STUB_PROVIDER] == "REQUIRES_HUMAN_INTERVENTION", provider_status

    _seed_blocked(fleet, repo_id=_STUB_CONSUMER, blocked_by=[_STUB_PROVIDER])

    # --- step 6 (real unblock) + step 8 (real TRANSFORM re-dispatch with the trigger armed) ---
    resumed = runner.invoke(app, [*base_args(fleet), "--json", "resume", "--stub-blocked"])
    assert resumed.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION, resumed.output
    resume_payload = json.loads(resumed.stdout)
    assert resume_payload["unblocked_dependents"]["unblocked"] == [
        {
            "repo_id": _STUB_CONSUMER,
            "removed": [_STUB_PROVIDER],
            "remaining": [],
            "floor": "TRANSFORM",
        }
    ], resume_payload["unblocked_dependents"]

    transform_statuses = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 2"))
    assert transform_statuses[_STUB_CONSUMER] == "DEGRADED", transform_statuses
    assert transform_statuses[_STUB_PROVIDER] == "REQUIRES_HUMAN_INTERVENTION", transform_statuses

    stub_rows = query(
        fleet,
        "SELECT state, stub_fidelity, pinned_version, consumer_repo_id, provider_repo_id, "
        "       max_revalidation_rounds, revalidation_round "
        "  FROM stubs WHERE run_id = (SELECT run_id FROM runs) AND stub_coord_key = ?",
        (_STUB_COORD_KEY,),
    )
    assert stub_rows == [
        ("ACTIVE", "PUBLISHED_ARTIFACT", "2.0.1", _STUB_CONSUMER, _STUB_PROVIDER, 2, 0)
    ], stub_rows

    # --- BUILD/VERIFY admit the DEGRADED row (this leg's `_eligible_build_units`/`_gated_
    # members` widening) and correct their OWN rows to DEGRADED too (this leg's new per-phase
    # `stub_degrade_transform` calls) ---
    built = build(fleet, "--no-sandbox", json_output=False)
    # Exit 7, not 0: a `DEGRADED` repo alone (no RHI at THIS phase) still reports "a human is
    # needed" per D93/§3.5.1 point 5 — `acme-lib-py` itself never reaches BUILD at all (excluded
    # from `_eligible_build_units`'s domain, asserted below), so the ONLY reason for exit 7 here
    # is `acme-app-py`'s own DEGRADED status.
    assert built.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION, built.output
    build_statuses = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 3"))
    assert build_statuses[_STUB_CONSUMER] == "DEGRADED", build_statuses
    assert _STUB_PROVIDER not in build_statuses, "the RHI provider must never reach BUILD"

    verified_result = verify(fleet, json_output=False)
    assert verified_result.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION, verified_result.output
    verify_statuses = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 4"))
    assert verify_statuses[_STUB_CONSUMER] == "DEGRADED", verify_statuses

    report_rows = query(
        fleet,
        "SELECT payload FROM findings WHERE kind = 'VerificationReport' AND repo_id = ?",
        (_STUB_CONSUMER,),
    )
    assert len(report_rows) == 1, report_rows
    report_body = json.loads(str(report_rows[0][0]))["report"]
    assert report_body["equivalence"] == "STUB_LIMITED", report_body
    assert report_body["verified_against_stubs"] == [_STUB_COORD_KEY], report_body
    assert report_body["stub_fidelity"] == {_STUB_COORD_KEY: "PUBLISHED_ARTIFACT"}, report_body

    # --- §12.37 clause 1, in full: C is DEGRADED, one ACTIVE/PUBLISHED_ARTIFACT stubs row, and a
    # VerificationReport whose equivalence is STUB_LIMITED naming P's coordinate. Proven above.

    # --- "the transition INTO the already-proven reconciliation path" (D80, unmodified): a
    # `StubRecord` CONSTRUCTED to match every field of the REAL row just asserted above (state,
    # stub_fidelity, pinned_version, consumer/provider ids, max_revalidation_rounds,
    # revalidation_round -- all seven asserted equal to the real row immediately above, none
    # invented) is fed to `orchestrator.stubs.supersede` exactly as `tests/test_stubs.py::
    # test_t1_fires_on_succeeded_and_merged` feeds its own hand-built one. Not a read-back
    # through `StubRecord.model_validate` (D80's own reconstruction shape, `cli._stub_reconcile_
    # inputs`) -- constructing it directly here is enough to prove `supersede()` accepts and
    # correctly transitions the exact values this run produced.
    record = StubRecord(
        run_id=UUID(str(run_id_of(fleet))),
        coord_key=_STUB_COORD_KEY,
        provider_repo_id=_STUB_PROVIDER,
        consumer_repo_ids=[_STUB_CONSUMER],
        fidelity=StubFidelity.PUBLISHED_ARTIFACT,
        pinned_version="2.0.1",
        state=StubState.ACTIVE,
        max_revalidation_rounds=2,
        rounds_spent=0,
    )
    decisions = supersede(
        record, ProviderFacts(_STUB_PROVIDER, RepoStatus.SUCCEEDED, PrState.MERGED)
    )
    assert len(decisions) == 1, decisions
    decision = decisions[0]
    assert decision.transition is StubTransition.T1, decision
    assert (decision.from_state, decision.to_state) == (StubState.ACTIVE, StubState.SUPERSEDED)
    assert decision.consumer_status is RepoStatus.DEGRADED, decision
