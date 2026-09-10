"""The event stream as an ARTIFACT: does a real run leave `logs/events-<run_id>.jsonl` on disk?

`tests/test_obs.py` already proves the machinery works when something constructs it — an
`EventEmitter` handed a path writes a redacted line, `configure(json_path=...)` opens a sink. What
nothing proved is that any *code path in the harness* ever does either. Measured on the pre-wiring
tree: `EventEmitter` was never constructed anywhere in `src/` (`.emit(` returned zero hits), all
six `log_configure` call sites in `src/fleet/cli.py` omitted `json_path=`, and the only two events
that reached the `events` table did so through direct `repository.append_event` calls that bypass
the emitter entirely. So ADR-0012's stream and §8's file did not exist, and `docs/SPEC.md` §10's
`jq … logs/events-<run_id>.jsonl` recipe had nothing to read.

That is a defect a unit test cannot see, because a unit test supplies the wiring it is meant to be
checking. Every test in this file therefore drives the **real CLI** through `runner.invoke` and
then looks at the filesystem, and each one names the distinct leg of the wiring it is the unique
discriminator for:

* `test_a_pr_sync_run_writes_the_event_stream_file` — the file exists at all.
* `test_pr_merged_reaches_the_stream_and_not_only_the_events_table` — the emitter leg. Fails if
  `log_configure` got its `json_path` but the `append_event` caller was left direct.
* `test_the_log_pipeline_points_at_the_runs_stream_file` — the `log_configure` leg. Fails if the
  emitter was constructed but no worker's `ctx.log` line can ever reach the stream.
* `test_a_credential_in_a_pr_url_survives_into_neither_sink` — §12.20 at the new egress. This one
  discriminates on the SQL sink independently of the file: `_pr_sync_impl` built its payload with
  a bare `json.dumps` and `SqliteStateRepository.append_event` does not redact, so the PAT reached
  `events.payload` in clear on the pre-wiring tree.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fleet import cli as cli_module
from fleet.cli import app
from fleet.obs import log as logmod
from tests.test_cli import (
    RUN_ID,
    _MergedForge,
    _seed_pr_record,
    base_args,
    workspace,
)

# `workspace` is imported for its side effect of being a fixture in this module's namespace.
__all__ = ["workspace"]

runner = CliRunner()

#: A credential shaped exactly like the one §12.20 names, carried in the position an operator
#: really does end up with one: the forge URL persisted on the `PullRequestDraft`.
PAT = "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"
CLEAN_URL = "https://github.invalid/acme/monorepo/pull/1"
LEAKY_URL = f"https://x-access-token:{PAT}@github.invalid/acme/monorepo/pull/1"

#: §12.20's own grep, verbatim, rather than a substring somebody remembered to think of.
LEAK_GREP = re.compile(
    r"github_pat_|ghp_|xox[baprs]-|AKIA[0-9A-Z]{16}|sk-ant-|sk-[A-Za-z0-9]{32,}"
    r'|"private_key_id"|://[^/\s:@]+:[^/\s@]+@'
)


@pytest.fixture(autouse=True)
def _restore_the_global_log_pipeline() -> Iterator[None]:
    """`configure()` is process-global; a run here must not leave a `tmp_path` handle wired in.

    Without this, the sink these tests open stays installed for every later test in the session,
    writing into an unlinked inode once `tmp_path` is reaped — telemetry silently going nowhere is
    the exact failure `obs/log.py` was written about, and a test file must not create it.
    """
    try:
        yield
    finally:
        logmod.shutdown()
        logmod.configure()


def _stream_path(root: Path) -> Path:
    """Where §8's directory listing says the stream lives. Spelled out, not imported.

    Deliberately NOT `events_jsonl_path(...)`: a test that asks the implementation where it put
    the file cannot catch the implementation putting it somewhere §8 does not name.
    """
    return root / "logs" / f"events-{RUN_ID}.jsonl"


def _sync_run(workspace: Path, monkeypatch: pytest.MonkeyPatch, *, url: str | None) -> None:
    """One real `fleet pr --sync`, optionally with one pollable PR to find MERGED."""
    from fleet import cli

    if url is not None:
        _seed_pr_record(workspace / "state" / "fleet.db", "acme-commons", state="DRAFTED", url=url)
    monkeypatch.setattr(cli, "GH_RUNNER", _MergedForge())
    result = runner.invoke(app, [*base_args(workspace), "pr", "--sync"])
    assert result.exit_code == 0, result.output


def _lines(path: Path) -> list[dict[str, object]]:
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_a_pr_sync_run_writes_the_event_stream_file(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§8: a run produces `logs/events-<run_id>.jsonl`, and every line of it is JSON.

    Catches, that the pre-wiring tree missed: no code path in `src/` produced this file, so §10's
    `jq 'select(.event==…)' logs/events-<run_id>.jsonl` recipe was a recipe over nothing. The
    per-line `json.loads` is not decoration — two writers append to this one file (ADR-0012's
    "one event pipeline, two renderers"), and a file that is only *sometimes* one-object-per-line
    is unreadable by `jq` in exactly the run where an operator needs it.
    """
    _sync_run(workspace, monkeypatch, url=CLEAN_URL)

    path = _stream_path(workspace)
    assert path.is_file(), f"ADR-0012's stream was never written; {path} does not exist"
    assert _lines(path), "the stream file exists but is empty after a run that merged a PR"


def test_pr_merged_reaches_the_stream_and_not_only_the_events_table(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `pr_merged` event is on the JSONL line *and* in `events`, under one `event_uid`.

    Catches: an implementation that opens the sink but leaves `_pr_sync_impl` writing straight to
    `repository.append_event`. That shape passes the test above (the file exists, `log_configure`
    made it) and still leaves §10's `jq` recipe unable to see the one event §3.4 step 5 exists to
    produce. The shared `event_uid` is what makes this "the same event in two sinks" rather than
    "two events that happen to share a name" — it is also the idempotency key §11.7 replays on,
    so a stream whose uid does not match the row cannot be replayed into it.
    """
    _sync_run(workspace, monkeypatch, url=CLEAN_URL)

    streamed = [line for line in _lines(_stream_path(workspace)) if line["event"] == "pr_merged"]
    assert len(streamed) == 1, "pr_merged did not reach the JSONL stream"

    conn = sqlite3.connect(workspace / "state" / "fleet.db")
    try:
        rows = conn.execute(
            "SELECT event_uid, seq, repo_id FROM events WHERE event = 'pr_merged'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, "pr_merged did not reach the events table"
    assert streamed[0]["event_uid"] == rows[0][0], "the two sinks disagree about which event"
    assert streamed[0]["repo_id"] == rows[0][2] == "acme-commons"
    assert rows[0][1] >= 1, "seq was not allocated in-statement"


def test_the_log_pipeline_points_at_the_runs_stream_file(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run installs the JSONL sink, so a line logged through the pipeline lands in the stream.

    Catches: an implementation that constructs the `EventEmitter` but never passes `json_path=` to
    `log_configure`. ADR-0012 is explicit that this is "one event pipeline, two renderers" — the
    console renderer and the JSONL file are the same stream — so with the sink missing, every
    `ctx.log.info(...)` a worker emits (`transform_checkpoint_rejected`, the failover warnings,
    `EventEmitter._surface`'s own `event_emit_failed`) exists only on a console nobody captured.

    Driven with **nothing pollable**, which is what makes it the unique discriminator: no event is
    emitted at all, so the emitter cannot be what puts anything in this file. And the probe goes
    through the process-global pipeline the run configured, not through a logger this test built,
    which is the difference between "a sink can be opened" and "the run opened one".

    Deliberately NOT asserted: that the file exists *before* the probe. `configure()` opens its
    sink eagerly (`obs/log.py`, `path.open("a", …)`), so on this tree it does — but that is a
    property of `_Sink`, not of this wiring, and §12.18 requires the sibling
    `logs/errors-<run_id>.jsonl` to exist **if and only if** a recoverable error occurred. A lane
    making `_Sink` lazy to satisfy that "only if" must not be reddened by this file; the probe
    lands either way, so nothing is lost by leaving eagerness free.
    """
    _sync_run(workspace, monkeypatch, url=None)

    path = _stream_path(workspace)
    logmod.get_logger("tests.stream").info("probe_after_the_run", url=LEAKY_URL)
    logmod.shutdown()

    assert path.is_file(), "the run never opened §8's JSONL sink"
    written = _lines(path)
    assert [line["event"] for line in written] == ["probe_after_the_run"], (
        "the pipeline the run configured does not write to the run's stream (or the emitter "
        f"wrote to it in a run with nothing to poll): {written}"
    )
    assert LEAK_GREP.search(path.read_text(encoding="utf-8")) is None
    assert "«redacted" in str(written[0]["url"])


def test_a_credential_in_a_pr_url_survives_into_neither_sink(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§12.20 at the new egress: the PAT is in neither the JSONL line nor `events.payload`.

    Catches two separate things the pre-wiring tree missed. The obvious one: a new file sink is a
    new place for a credential to land, and §11.4 names `logs/` an egress path in its own right.
    The one that is not obvious, and that fails **independently of whether the file exists**:
    `_pr_sync_impl` built its event payload with a bare `json.dumps({"url": status.url, …})`, and
    `SqliteStateRepository.append_event` does not redact — it takes `row.payload` as a finished
    string. `obs/events.py` is the redaction boundary for events ("redact FIRST, then serialise"),
    so a caller that goes around it writes the credential into `events.payload` in clear. §12.20's
    acceptance criterion runs its grep over `SELECT payload FROM events`; on the pre-wiring tree
    that grep found this.

    The redacted-URL assertion is the other half: a payload that dropped the field entirely would
    also pass a leak grep, and would take the PR link an operator needs with it.
    """
    _sync_run(workspace, monkeypatch, url=LEAKY_URL)

    on_disk = _stream_path(workspace).read_text(encoding="utf-8")
    assert LEAK_GREP.search(on_disk) is None, "§12.20's grep found a credential in logs/"

    conn = sqlite3.connect(workspace / "state" / "fleet.db")
    try:
        payloads = [str(row[0]) for row in conn.execute("SELECT payload FROM events")]
    finally:
        conn.close()
    assert payloads, "no event row to check"
    joined = "\n".join(payloads)
    assert LEAK_GREP.search(joined) is None, "§12.20's grep found a credential in events.payload"
    # Parsed, not substring-matched: `json.dumps` escapes `«` to `\\u00ab`, so a raw-text check
    # for the placeholder reads a correctly redacted payload as a vanished field.
    stored_url = str(json.loads(payloads[0])["url"])
    assert "«redacted" in stored_url, "the url field vanished instead of being redacted"
    assert "github.invalid/acme/monorepo/pull/1" in stored_url, "the PR link did not survive"


def test_a_dropped_pr_merged_event_fails_the_run_loudly_and_names_what_was_lost(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the `events` sink refuses the event, `pr --sync` exits 1 with a message, not a stall.

    This is the one test here that is not about the pre-wiring tree; it is about the regression
    routing through the emitter would otherwise ship. `EventEmitter.emit` **never raises** — that
    carve-out is written, at length, for "the caller is a worker mid-transform and the failing
    operation is telemetry". `pr_merged` is not telemetry: `docs/SPEC.md:1529` designates it the
    signal that "unblocks the dependent's Phase 4 precondition and fires T1", and the direct
    `repository.append_event` call this replaced propagated its failure.

    What the guard does NOT do, written down because this docstring claimed otherwise until
    round L measured it: **nothing reads the `events` table for `pr_merged`.** The only
    `FROM events` sites in `src/` are `state/repository.py`'s per-run iterator, `fleet doctor`'s
    count summary and the GC. §3.4 step 5's stacking gate reads the `findings` row of kind
    `PullRequest` that `_write_pr_record` writes *before* the emit (`cli._pr_records`), so a
    swallowed event blocks no dependent today. What it loses is the record SPEC designates as
    the signal, while the operator is told the sync succeeded — which is why the run fails loud.

    Discriminating mutation, measured rather than argued: deleting the `if not outcome.stored:`
    guard from `_pr_sync_impl` leaves the other four tests in this file GREEN and reddens only
    this one, with `exit_code` 0 and `polled 1 open PR(s); 1 newly MERGED` on stdout — the run
    reporting the merge it did not record.

    The assertion is on the *mapped* failure, not merely on a non-zero exit: §10 gives the CLI
    eleven codes so CI can tell them apart, and `_mapped_errors` is what turns a typed error into
    one of them. The pre-wiring tree fails this too, for a different reason — `append_event`'s
    `RepositoryError` is not in `_mapped_errors`' funnel, so it escaped as a traceback.
    """
    from fleet.state.repository import RepositoryError, SqliteStateRepository

    async def _refuse(self: object, row: object) -> int:
        raise RepositoryError("disk is full")

    monkeypatch.setattr(SqliteStateRepository, "append_event", _refuse)

    from fleet import cli

    _seed_pr_record(
        workspace / "state" / "fleet.db", "acme-commons", state="DRAFTED", url=CLEAN_URL
    )
    monkeypatch.setattr(cli, "GH_RUNNER", _MergedForge())
    result = runner.invoke(app, [*base_args(workspace), "pr", "--sync"])

    assert result.exit_code == 1, f"a dropped pr_merged was not loud: {result.output}"
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"the failure escaped `_mapped_errors` as a traceback: {result.exception!r}"
    )
    assert "acme-commons" in result.output, "the failure does not name the repo"
    assert "pr_merged" in result.output, "the failure does not name what was lost"


# ======================================================================================
# §12.18 — every real `RunContext(` site must pass `root=`
# ======================================================================================


def _run_context_call_sites() -> list[ast.Call]:
    """Every `RunContext(...)` call in `src/fleet/cli.py`, found by walking the real source —
    not by grepping the six call sites this docstring already knows about, which is exactly the
    kind of hand-maintained list Guardrail 6 warns rots ("derive from the body").
    """
    source = Path(cli_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "RunContext"
    ]


def test_every_run_context_call_site_in_cli_passes_root(workspace: Path) -> None:
    """§12.18's JSONL/errors-sink routing is dead on any `RunContext(` site missing `root=` —
    `RunContext.__post_init__` builds the run's `EventEmitter` with `jsonl_path=None` and
    `errors_jsonl_path=None` whenever `root is None` (see that field's own docstring), so a run
    assembled at such a site would still write `llm_call` to the `events` table (`sink=
    self.repository` is unconditional) but NEVER to `logs/events-<run_id>.jsonl` or
    `logs/errors-<run_id>.jsonl` — silently, with no exception and no other test failure, because
    `root` is `Path | None = None` by design (the same optional-collaborator shape as
    `llm_cache`/`llm_policy`/`backends` on this dataclass).

    Only ONE of `cli.py`'s six real sites (`_run_scan_wave`, `_run_transform_wave`,
    `_run_build_wave`, `_run_verify_wave`, `_emit_prs`, `_run_revalidation_claims_impl` — the
    last added by round VI task 109's §12.39-B1 cost instrumentation on the REVALIDATE path,
    after this test's "five" count was written) is exercised end-to-end for this by
    `test_fleet_pr_llm_calls_reach_the_event_stream_with_every_spec_1218_field` in
    `tests/test_pr_e2e.py` (the `_emit_prs` one) — a full e2e per site would mean standing up a
    real scan/transform/build wave five more times just to prove one keyword argument survived,
    which is disproportionate to what it guards. This structural sweep is the cheap alternative:
    it does not prove `root=settings.root` resolves to the right VALUE at any one site (the e2e
    test above already proves that for the one it drives), but it does prove every site still
    PASSES the keyword at all, which is exactly what a future edit dropping it from any of the
    other four would silently break.

    The count assertion is not decorative: a `RunContext(` site added in the future that this
    sweep does not know about must fail LOUD (a 6th, unlisted site) rather than silently pass by
    virtue of not being checked — the same reasoning `test_run_context_llm_policy.py`'s "five,
    AST-counted" already documents in prose for `policy=None`, made executable here for `root=`.
    """
    sites = _run_context_call_sites()
    assert len(sites) == 6, (
        f"expected exactly 6 `RunContext(` call sites in cli.py, found {len(sites)} at lines "
        f"{[node.lineno for node in sites]} — update this sweep's expectation deliberately if a "
        "site was added or removed, don't just raise the number to make it pass"
    )

    missing = [node.lineno for node in sites if not any(kw.arg == "root" for kw in node.keywords)]
    assert missing == [], (
        f"RunContext( at cli.py:{missing} does not pass root=, so that run silently loses "
        "§12.18's logs/events-<run_id>.jsonl and logs/errors-<run_id>.jsonl routing for "
        "llm_call — with no exception and no other test catching it (RunContext.root's own "
        "field docstring names this exact failure mode)"
    )
