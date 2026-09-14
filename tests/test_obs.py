"""Behaviour tests for `src/fleet/obs/` — redaction, the event stream, the log pipeline.

Every assertion here pins a property whose *absence* is silent. An unredacted log line does not
raise; it writes a live GitHub PAT into `logs/` and nobody notices until the token is used. A
`seq` allocated in Python does not look wrong; it raises `IntegrityError` on the second
concurrent emitter and kills a worker that was doing nothing wrong. An emit that propagates its
own failure does not look wrong either; it aborts a migration because a disk filled up.

So the assertions are behavioural — a real temp database, real concurrency, the actual emitted
string — and the leak checks run §12.20's own grep regex over the output rather than looking for
a substring somebody remembered to think of.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import sys
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fleet.models.enums import Phase
from fleet.obs import events as events_mod
from fleet.obs import log as logmod
from fleet.obs.events import EmitResult, EventEmitter
from fleet.obs.redact import (
    ENTROPY_MIN_BITS,
    redact,
    redact_mapping,
    redact_text,
    redaction_counts,
    reset_redaction_counts,
)
from fleet.state import db as dbmod
from fleet.state.db import StateWriter, connect_ro, initialize_database
from fleet.state.repository import EventRow, SqliteStateRepository

RUN = "22222222-2222-4222-8222-222222222222"
REPO = "acme-commons"
NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)

#: A real-shaped PAT in a real-shaped mirror remote. This exact string is what 251 mirror
#: configs in this environment carry, and one unredacted log line publishes it.
PAT = "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"
REMOTE_URL = f"https://oauth2:{PAT}@gitea.local:3001/redmage/acme-commons.git"

#: SPEC §12.20 verbatim: this must find nothing in `logs/`, `artifacts/`, `migration_state.json`.
LEAK_GREP = re.compile(
    r"github_pat_|ghp_|xox[baprs]-|AKIA[0-9A-Z]{16}|sk-ant-|sk-[A-Za-z0-9]{32,}"
    r'|"private_key_id"|://[^/\s:@]+:[^/\s@]+@'
)


@pytest.fixture(autouse=True)
def _clean_obs_state() -> Iterator[None]:
    """Counters and the write slot are process-wide; a leak makes the next test lie."""
    reset_redaction_counts()
    logmod.reset_write_stats()
    yield
    logmod.shutdown()
    dbmod._release_write_slot()


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    """The console sink, captured, so a test can assert on the bytes that were actually written."""
    stream = io.StringIO()
    logmod.configure("DEBUG", stream=stream)
    yield stream
    logmod.shutdown()


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "fleet.db"
    await initialize_database(path)
    return path


@pytest.fixture
async def store(db_path: Path) -> AsyncIterator[SqliteStateRepository]:
    """Wired as the runner wires it: one `StateWriter`, one `mode=ro` handle (§11.5)."""
    async with StateWriter(db_path, owner="test-obs-writer") as writer:
        read_conn = await connect_ro(db_path)
        try:
            repo = SqliteStateRepository(writer=writer, read_conn=read_conn)
            await repo.upsert_run(
                RUN, started_at=NOW, config_sha256="a" * 64, harness_version="0.1.0"
            )
            await repo.upsert_repo(REPO, name=REPO, url="https://example.invalid/x.git", now=NOW)
            yield repo
        finally:
            await read_conn.close()


class _FailingSink:
    """A sink whose disk is full. Injected to prove telemetry cannot kill its caller."""

    def __init__(self) -> None:
        self.calls = 0

    async def append_event(self, row: EventRow) -> int:
        self.calls += 1
        raise OSError(28, "No space left on device")


# ======================================================================================
# redaction — the security-critical half
# ======================================================================================


def test_pat_in_remote_url_never_reaches_a_log_line(log_stream: io.StringIO) -> None:
    """A `github_pat_…` in a mirror's origin URL must not survive into the log output.

    Why: 251 of the mirrors in this environment embed a plaintext PAT in their remote URL, and a
    single `log.error("clone failed", url=remote)` publishes a live credential to `logs/`. The
    call site here never mentions redaction — that is the point: it happens at the boundary.
    """
    logmod.get_logger("test").error("clone_failed", url=REMOTE_URL, repo_id=REPO)
    written = log_stream.getvalue()

    assert PAT not in written
    assert "github_pat_" not in written
    assert LEAK_GREP.search(written) is None, "SPEC §12.20's own grep found a secret in the log"
    # Still debuggable: the host, the repo and the event survived.
    assert "gitea.local" in written
    assert "«redacted" in json.loads(written)["url"]


async def test_pat_never_reaches_an_event_payload(tmp_path: Path) -> None:
    """The same PAT, through the *event* boundary: JSONL line and `events.payload` both clean.

    Why: §11.4 lists `events` and `logs/` as separate egress paths. Redacting one and not the
    other is the leak that a "we redact in the logger" implementation ships with.
    """
    jsonl = tmp_path / "logs" / f"events-{RUN}.jsonl"
    emitter = EventEmitter(run_id=RUN, jsonl_path=jsonl)

    result = await emitter.emit(
        "clone_failed",
        level="error",
        repo_id=REPO,
        phase=Phase.SCAN,
        payload={"remote": REMOTE_URL, "attempt": 2},
        now=NOW,
    )

    assert result.streamed
    written = jsonl.read_text(encoding="utf-8")
    assert PAT not in written
    assert LEAK_GREP.search(written) is None
    line = json.loads(written.splitlines()[0])
    assert "«redacted" in line["payload"]["remote"]
    assert line["payload"]["attempt"] == 2  # non-secret fields survive intact


def test_redaction_recurses_and_never_mutates_the_caller() -> None:
    """Nesting is the normal shape of an event payload, and the caller still owns its object.

    Why: a redactor that edited in place would silently change the URL a worker is about to clone
    from — a worse bug than the leak it was preventing.
    """
    payload: dict[str, object] = {
        "repos": [
            {"id": REPO, "remotes": [{"origin": REMOTE_URL}]},
            {"id": "acme-billing", "remotes": [{"origin": "https://gitea.local/x.git"}]},
        ]
    }
    original = json.dumps(payload, sort_keys=True)

    cleaned = redact_mapping(payload)  # type: ignore[arg-type]

    assert json.dumps(payload, sort_keys=True) == original, "redact() mutated its argument"
    deep = cleaned["repos"][0]["remotes"][0]["origin"]  # type: ignore[index]
    assert PAT not in str(deep)
    assert LEAK_GREP.search(json.dumps(cleaned)) is None
    # The clean sibling is untouched, so a diff of two payloads still means something.
    assert cleaned["repos"][1]["remotes"][0]["origin"] == "https://gitea.local/x.git"  # type: ignore[index]


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345", "authorization"),
        ("curl -H 'Authorization: token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345'", "github_classic"),
        ("export ANTHROPIC_API_KEY=sk-ant-api03-AAAAbbbbCCCCddddEEEEffffGGGG", "anthropic"),
        ("aws_access_key_id = AKIAIOSFODNN7EXAMPLE", "aws_key"),
        ("xoxb-1234567890-abcdefghij", "slack"),
    ],
)
def test_credential_shapes_are_redacted(text: str, kind: str) -> None:
    """Bearer tokens, `Authorization:` headers and API keys are all credentials, not strings.

    Why: §11.4's threat is "a token in tool output", and tool output is where a curl command line
    and an `export` line live.
    """
    cleaned = redact_text(text)
    assert LEAK_GREP.search(cleaned) is None
    assert "«redacted:" in cleaned
    assert kind in redaction_counts()


@pytest.mark.parametrize(
    "text",
    [
        f"config_sha256={'a' * 64}",
        "commit=0123456789abcdef0123456789abcdef01234567",
        "dest_path=/mnt/storage/gitea/data/git/repositories/redmage/omninexus.git",
        "branch=migrate/acme-commons-phase-3-rebuild-of-the-manifest-adapter",
        "token_count=180000",
    ],
)
def test_no_over_redaction_of_innocuous_long_values(text: str) -> None:
    """A long value is not a secret. A log that redacts digests and paths cannot be debugged with.

    Why: §11.4 gates the generic rule on entropy (≥4.0 bits/char) *and* a key-ish word precisely
    so `config_sha256` (~3.9 bits/char over 16 symbols) and a long path stay readable. Rule 9:
    the reason this test exists is that over-redaction destroys the artifact it protects.
    """
    assert redact_text(text) == text
    assert redaction_counts() == {}


def test_entropy_gate_still_catches_a_real_high_entropy_secret() -> None:
    """The gate is a filter, not an exemption: a key-ish word plus a random token still goes."""
    secret = "Xq7$vB2!nR9zLm4Wp0Ts6Yc1Ke8Ah3Gd"  # noqa: S105 — a fixture, not a credential
    cleaned = redact_text(f'"client_secret": "{secret}"')
    assert secret not in cleaned
    assert redaction_counts().get("high_entropy") == 1
    assert ENTROPY_MIN_BITS == 4.0


def test_env_sourced_secret_is_redacted_even_with_no_recognisable_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value the environment declared secret is secret, whatever it looks like.

    Why: §9 says API keys come from the environment only. A locally-served vLLM backend's key can
    be any string at all, so pattern matching alone would never catch it (ADR-0023).
    """
    monkeypatch.setenv("FLEET_TEST_API_KEY", "plaintiff-oyster-lantern")
    cleaned = redact_text("calling backend with key plaintiff-oyster-lantern for role HEAVY")
    assert "plaintiff-oyster-lantern" not in cleaned
    assert "«redacted:env.FLEET_TEST_API_KEY:" in cleaned


def test_secret_dict_key_redacts_a_short_value() -> None:
    """`{"api_key": "hunter2"}` is a secret at any length — the key name is the evidence."""
    cleaned = redact({"api_key": "hunter2", "token_count": 42, "repo_id": REPO})
    assert cleaned["api_key"] != "hunter2"
    assert cleaned["token_count"] == 42, "a count is not a credential"
    assert cleaned["repo_id"] == REPO


def test_same_secret_gets_the_same_fingerprint_and_is_not_recoverable() -> None:
    """One stable placeholder per value: correlatable across the log, never reversible (§11.4)."""
    first = redact_text(f"url={REMOTE_URL}")
    second = redact_text(f"other url={REMOTE_URL}")
    fingerprints = {line.split(":")[-1] for line in (first, second)}
    assert len(fingerprints) == 1
    assert PAT not in first + second


# ======================================================================================
# the log pipeline
# ======================================================================================


def test_log_output_is_json_with_stable_key_order(log_stream: io.StringIO) -> None:
    """Valid JSON, fixed head order, the tail sorted — so a golden log diffs cleanly (§10)."""
    log = logmod.get_logger("test").bind(run_id=RUN, repo_id=REPO)
    log.info("phase_started", zulu=1, alpha=2, phase=1)
    log.info("phase_started", alpha=2, phase=1, zulu=1)

    lines = log_stream.getvalue().splitlines()
    first, second = (json.loads(line) for line in lines)
    assert list(first) == [
        *("timestamp", "level", "event", "run_id", "repo_id", "phase"),
        "alpha",
        "zulu",
    ]
    # Same facts in a different kwargs order must render identically apart from the timestamp.
    first.pop("timestamp"), second.pop("timestamp")
    assert first == second


def test_configure_writes_the_jsonl_sink_and_the_console(tmp_path: Path) -> None:
    """§8: `logs/events-<run_id>.jsonl` and the console are one pipeline, not two configs."""
    stream = io.StringIO()
    path = tmp_path / "logs" / "run.jsonl"
    logmod.configure("INFO", json_path=path, stream=stream)
    logmod.get_logger("test").info("wave_started", url=REMOTE_URL)
    logmod.shutdown()

    on_disk = path.read_text(encoding="utf-8")
    assert json.loads(on_disk)["event"] == "wave_started"
    assert on_disk == stream.getvalue()
    assert LEAK_GREP.search(on_disk) is None


def test_get_logger_configures_rather_than_logging_unredacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unconfigured pipeline has no redaction processor, so it must not be reachable."""
    monkeypatch.setattr(logmod, "_configured", False)
    assert not logmod.is_configured()
    logmod.get_logger("test")
    assert logmod.is_configured()


# ======================================================================================
# the log pipeline: a dead sink
#
# These pin the inversion CLAUDE.md Rule 11 is *not* about. Everywhere else a failure must be
# loud; here the failing operation is telemetry and the caller is a worker mid-migration, so a
# raising sink does not surface a problem, it manufactures one. That is not hypothetical: a
# stream that outlived its file raised `I/O operation on closed file` out of a `log.warning()`
# in a phase's error path, and five repos that had done nothing wrong were written down as
# REQUIRES_HUMAN_INTERVENTION. `obs.events` already refuses to do this to its caller; below is
# the same guarantee for `obs.log`, with the same swallow-*and*-surface bookkeeping.
# ======================================================================================


def _phase_that_logs_on_its_way_out() -> str:
    """A stand-in for the real caller: a phase whose last act before succeeding is to log."""
    logmod.get_logger("test").warning("transient_retry_exhausted", attempt=3)
    return "SUCCEEDED"


def test_a_closed_console_stream_does_not_raise_into_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE headline: a dead sink must not be able to fail the work it is describing.

    The incident this reproduces cost five healthy repos their status. The assertion is not that
    the line was written — it was not, and cannot be — but that the caller *finished*.
    """
    dead = io.StringIO()
    logmod.configure("DEBUG", stream=dead)
    dead.close()
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    assert _phase_that_logs_on_its_way_out() == "SUCCEEDED"


def test_a_refused_write_is_counted_and_retained_rather_than_hidden() -> None:
    """Swallowed is not the same as hidden (Rule 11): the drop must be legible to an operator.

    Same bookkeeping as `EventEmitter.failed_emits`/`failures`, so one habit reads both.
    """
    dead = io.StringIO()
    logmod.configure("DEBUG", stream=dead)
    dead.close()
    logmod.get_logger("test").info("wave_started")

    stats = logmod.write_stats()
    assert not stats.ok
    assert stats.failed_writes >= 1
    assert stats.failures[-1].sink == "console:stream"
    assert "ValueError" in stats.failures[-1].error


def test_a_line_that_reaches_no_sink_at_all_is_counted_as_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`dropped_lines` is the number that means telemetry was actually LOST, not just degraded.

    A refusal that the fallback rescued is data an operator still has; this counter must only
    move when nowhere took the line, or it is noise nobody will act on.
    """
    dead, dead_stderr = io.StringIO(), io.StringIO()
    logmod.configure("DEBUG", stream=dead)
    dead.close(), dead_stderr.close()
    monkeypatch.setattr(sys, "stderr", dead_stderr)

    logmod.get_logger("test").info("wave_started")

    stats = logmod.write_stats()
    assert stats.dropped_lines == 1
    assert {failure.sink for failure in stats.failures} == {"console:stream", "fallback:stderr"}


def test_the_fallback_sink_rescues_the_line_and_it_is_still_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§11.4 is not suspended by degradation: a fallback that leaked would be the worse bug.

    It cannot leak by construction — `redaction_processor` runs before the renderer, so what the
    tee re-sends is already a finished, redacted line — and this asserts the construction holds
    rather than trusting it, because an unredacted fallback line publishes a live PAT into
    whatever collects stderr.
    """
    rescue = io.StringIO()
    dead = io.StringIO()
    logmod.configure("DEBUG", stream=dead)
    dead.close()
    monkeypatch.setattr(sys, "stderr", rescue)

    logmod.get_logger("test").error("clone_failed", url=REMOTE_URL)

    written = rescue.getvalue()
    rendered = json.loads(written)
    assert rendered["event"] == "clone_failed"  # the line really was rescued
    assert PAT not in written
    assert LEAK_GREP.search(written) is None
    assert rendered["url"].startswith("https://«redacted:url_userinfo:")
    assert logmod.write_stats().dropped_lines == 0


def test_the_console_sink_follows_stderr_instead_of_capturing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The root cause itself: `configure()` must bind "this process's stderr", not the object.

    Capturing it is what let an in-process CLI runner's capture buffer — closed the instant the
    invocation returned — stay wired into the global pipeline and kill every later log call in
    the session. Re-resolving per write means a stream can be swapped or torn down underneath a
    long-lived process without poisoning it.
    """
    logmod.configure("DEBUG")  # no explicit stream: the console sink
    later = io.StringIO()
    monkeypatch.setattr(sys, "stderr", later)

    logmod.get_logger("test").info("wave_started")

    assert json.loads(later.getvalue())["event"] == "wave_started"
    assert logmod.write_stats().ok


def test_configure_after_a_stream_dies_restores_working_logging() -> None:
    """A long-lived process must be able to recover, not stay poisoned until it exits.

    Reconfiguring has to *drop* the dead handle, not add to it — otherwise every subsequent line
    keeps paying a refusal and `write_stats()` fills with noise from a sink nobody still wants.
    """
    dead = io.StringIO()
    logmod.configure("DEBUG", stream=dead)
    dead.close()
    logmod.get_logger("test").info("before")
    refusals = logmod.write_stats().failed_writes

    alive = io.StringIO()
    logmod.configure("DEBUG", stream=alive)
    logmod.get_logger("test").info("after")

    assert json.loads(alive.getvalue())["event"] == "after"
    assert logmod.write_stats().failed_writes == refusals  # the dead sink is gone, not retried


def test_a_dead_jsonl_sink_does_not_cost_the_console_line(tmp_path: Path) -> None:
    """The sinks fail independently — `obs.events` promises the same of its two sinks.

    `shutdown()` closing the file mid-process must not take the console down with it, or a
    tidy-up in one place silences logging everywhere.
    """
    stream = io.StringIO()
    logmod.configure("INFO", json_path=tmp_path / "logs" / "run.jsonl", stream=stream)
    logmod.shutdown()  # closes the JSONL handle; the pipeline is still bound to it

    logmod.get_logger("test").info("wave_started")

    assert json.loads(stream.getvalue())["event"] == "wave_started"
    assert logmod.write_stats().dropped_lines == 0
    assert [failure.sink for failure in logmod.write_stats().failures] == ["jsonl:run.jsonl"]


# ======================================================================================
# the event stream
# ======================================================================================


async def test_seq_is_allocated_in_statement_and_gapless_under_concurrency(
    store: SqliteStateRepository, tmp_path: Path
) -> None:
    """30 concurrent emits at one run produce seq 1..30 — no duplicates, no gaps, no crash.

    Why: two emitters both computing `MAX(seq)+1` in Python made the second collide on
    `UNIQUE (run_id, seq)` and raise `IntegrityError` — telemetry killing a worker. §6 fixes that
    by allocating inside the INSERT under `BEGIN IMMEDIATE`, and this asserts the fix holds under
    the concurrency that exposed it.
    """
    emitter = EventEmitter(run_id=RUN, sink=store, jsonl_path=tmp_path / "logs" / "e.jsonl")
    count = 30

    results = await asyncio.gather(
        *(
            emitter.emit("phase_started", repo_id=REPO, phase=Phase.SCAN, payload={"i": i})
            for i in range(count)
        )
    )

    seqs = [r.seq for r in results]
    assert all(r.ok for r in results)
    assert sorted(seqs) == list(range(1, count + 1))  # gapless, and every emit got its own
    assert len(set(seqs)) == count
    streamed = [row.seq async for row in store.iter_events(RUN)]
    assert streamed == list(range(1, count + 1))
    assert len((tmp_path / "logs" / "e.jsonl").read_text().splitlines()) == count


def test_emitter_never_computes_a_sequence_number() -> None:
    """The allocator lives in the INSERT, so this module must contain no `MAX(seq)` at all.

    Why: the concurrency test above would still pass if `seq` were computed in Python behind a
    single serialising writer — and would then break the moment a second writer existed. The
    absence of the read-then-compute is the property, so it is asserted directly.
    """
    source = Path(events_mod.__file__).read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    body = code.split('"""', 2)[-1]  # drop the module docstring, which quotes the SQL
    assert "MAX(" not in body.upper()
    assert "seq=0" in body  # the sentinel: the value passed in is ignored by the INSERT


async def test_re_emitting_the_same_event_uid_is_idempotent(store: SqliteStateRepository) -> None:
    """A replayed JSONL tail converges on the existing row: the conflict target is `event_uid`.

    Why: §11.7. Conflicting on `seq` instead would let a crash-resume mint a duplicate row under
    a fresh sequence number, and the `events` mirror would stop matching the JSONL stream.
    """
    emitter = EventEmitter(run_id=RUN, sink=store)
    first = await emitter.emit("phase_started", repo_id=REPO, event_uid="uid-replay")
    second = await emitter.emit("phase_started", repo_id=REPO, event_uid="uid-replay")

    assert first.seq == second.seq == 1
    assert [row.seq async for row in store.iter_events(RUN)] == [1]


async def test_emit_failure_does_not_propagate_to_the_caller(
    tmp_path: Path, log_stream: io.StringIO
) -> None:
    """A dead sink must not abort the migration that was merely reporting on itself.

    Why: this is the ONE place where not raising is correct (CLAUDE.md Rule 11 holds everywhere
    else). The failure is swallowed *and surfaced* — counted, retained and logged — so it is
    still observable; the caller's next statement simply runs.
    """
    sink = _FailingSink()
    emitter = EventEmitter(run_id=RUN, sink=sink, jsonl_path=tmp_path / "logs" / "e.jsonl")

    result = await emitter.emit("transform_finished", repo_id=REPO, payload={"files": 12})
    caller_kept_going = True  # the statement after the emit; unreachable if emit() raised

    assert caller_kept_going
    assert isinstance(result, EmitResult)
    assert result.seq is None and not result.stored
    assert result.streamed, "the healthy sink still took the event"
    # ...and the failure is surfaced three ways, not hidden.
    assert emitter.failed_emits == 1
    assert emitter.failures[-1].sink == "events"
    assert "No space left on device" in emitter.failures[-1].error
    assert "event_emit_failed" in log_stream.getvalue()


async def test_a_naive_now_fails_the_build_step_and_is_reported_without_raising(
    tmp_path: Path,
) -> None:
    """`_build_row` rejects a naive `now` outright ("every persisted instant is UTC (§11.5)"),
    and `emit()` wraps the whole `_build_row` call in its own `except Exception` block — the ONE
    failure branch nothing else in this file exercises. Every other failure test here injects a
    broken SINK (jsonl or SQL) after a row was already built; none makes row-BUILDING itself
    raise. A mutation deleting that try/except, or the naive-datetime check itself, would pass
    every other test in this file unnoticed while `emit()` stopped being exception-safe for a
    caller that ever passes a naive `now` (or any other value `_build_row` cannot handle)."""
    emitter = EventEmitter(run_id=RUN, jsonl_path=tmp_path / "logs" / "e.jsonl")
    naive = datetime(2026, 8, 9, 12, 0, 0)  # deliberately no tzinfo

    result = await emitter.emit("llm_call", payload={}, now=naive)

    assert result.seq is None
    assert result.stored is False
    assert result.streamed is False
    # NOTE (found while writing this test, not fixed here — out of this task's scope): unlike the
    # jsonl/sql failure paths below, `emit()`'s `except Exception` branch around `_build_row`
    # returns `EmitResult(...)` with no `failures=` argument, so `result.failures` is empty and
    # `result.ok` reads True here even though a failure genuinely occurred. The failure is still
    # counted and logged on the EMITTER itself (`failed_emits`/`failures` deque, asserted below)
    # -- it is not silently dropped -- but the per-call `EmitResult` disagrees with the emitter's
    # own bookkeeping about whether this call failed. Asserted as-is; flagged for the controller.
    assert result.failures == ()
    assert emitter.failed_emits == 1
    assert len(emitter.failures) == 1
    assert emitter.failures[-1].sink == "build"
    assert "naive" in emitter.failures[-1].error
    # the JSONL sink was never touched: the row never existed to write
    assert not (tmp_path / "logs" / "e.jsonl").exists()


async def test_emit_survives_an_unserialisable_payload(tmp_path: Path) -> None:
    """A payload the JSON encoder cannot take is rendered, not raised — and still redacted."""
    emitter = EventEmitter(run_id=RUN, jsonl_path=tmp_path / "logs" / "e.jsonl")
    result = await emitter.emit(
        "llm_call", payload={"started_at": NOW, "prompt": f"clone {REMOTE_URL}"}
    )

    assert result.streamed
    written = (tmp_path / "logs" / "e.jsonl").read_text(encoding="utf-8")
    assert LEAK_GREP.search(written) is None
    assert "2026-08-09" in written


async def test_emit_stores_a_redacted_payload_in_the_events_table(
    store: SqliteStateRepository,
) -> None:
    """§11.4: `events.payload` is post-redaction. The SQL mirror is an egress path too."""
    emitter = EventEmitter(run_id=RUN, sink=store)
    await emitter.emit("clone_failed", level="error", repo_id=REPO, payload={"url": REMOTE_URL})

    rows = [row async for row in store.iter_events(RUN)]
    assert len(rows) == 1
    assert LEAK_GREP.search(rows[0].payload) is None
    assert rows[0].level == "error"
    assert json.loads(rows[0].payload)["url"].startswith("https://«redacted")


# ======================================================================================
# §12.18 — the `logs/errors-<run_id>.jsonl` sink
# ======================================================================================


async def test_an_error_level_emit_reaches_both_jsonl_sinks_under_one_event_uid(
    tmp_path: Path,
) -> None:
    """The errors sink is a FILTER, not a route-away: an `error`-level line lands on BOTH
    `events-<run_id>.jsonl` and `errors-<run_id>.jsonl`, same `event_uid` — ADR-0012 still
    describes the main stream as carrying "every line", so ONLY duplicating (never diverting)
    keeps that description true once this sink exists.
    """
    emitter = EventEmitter(
        run_id=RUN,
        jsonl_path=tmp_path / "logs" / "events-x.jsonl",
        errors_jsonl_path=tmp_path / "logs" / "errors-x.jsonl",
    )
    result = await emitter.emit("llm_call", level="error", payload={"latency_ms": 12})

    main_line = json.loads((tmp_path / "logs" / "events-x.jsonl").read_text().strip())
    errors_line = json.loads((tmp_path / "logs" / "errors-x.jsonl").read_text().strip())
    assert main_line["event_uid"] == errors_line["event_uid"] == result.event_uid
    assert main_line == errors_line, "the errors sink must carry the SAME redacted line"


async def test_an_info_level_emit_never_creates_the_errors_sink_file(tmp_path: Path) -> None:
    """§12.18's "if and only if": the file's mere EXISTENCE is the "something went wrong" signal
    (ADR-0012), so a clean `info` line must leave it absent, not empty. Discriminates against an
    implementation that pre-creates the file (mirroring `obs/log.py`'s eager `_Sink`, which
    `tests/test_event_stream_wiring.py::test_the_log_pipeline_points_at_the_runs_stream_file`
    explicitly documents as the wrong shape for THIS file) or writes a blank line to it.
    """
    errors_path = tmp_path / "logs" / "errors-x.jsonl"
    emitter = EventEmitter(
        run_id=RUN,
        jsonl_path=tmp_path / "logs" / "events-x.jsonl",
        errors_jsonl_path=errors_path,
    )
    await emitter.emit("llm_call", level="info", payload={"latency_ms": 12})

    assert not errors_path.exists(), "an info-level event must not create the errors sink"


async def test_a_warning_level_emit_also_never_reaches_the_errors_sink(tmp_path: Path) -> None:
    """`backend_failover` events are `warn` (§11.8) and are a hop the fleet RECOVERED from, not
    the "recoverable error" ADR-0012's errors sink exists to flag — see `_ERROR_LEVELS`'s own
    docstring. Only `error`/`critical` qualify; this is the boundary one level below that.
    """
    errors_path = tmp_path / "logs" / "errors-x.jsonl"
    emitter = EventEmitter(
        run_id=RUN,
        jsonl_path=tmp_path / "logs" / "events-x.jsonl",
        errors_jsonl_path=errors_path,
    )
    await emitter.emit("backend_failover", level="warning", payload={})

    assert not errors_path.exists()


async def test_the_errors_sink_copy_is_redacted_exactly_like_the_main_stream(
    tmp_path: Path,
) -> None:
    """The filtered copy must not become a second, unredacted egress path (CLAUDE.md's redaction
    discipline, and `01b64d3`'s own commit message: this class of leak has happened once
    already). Both files are built from the SAME already-redacted `line` string in `_write_jsonl`,
    so this also catches an implementation that re-serialises the errors copy from `row` instead
    of reusing it.
    """
    errors_path = tmp_path / "logs" / "errors-x.jsonl"
    emitter = EventEmitter(
        run_id=RUN,
        jsonl_path=tmp_path / "logs" / "events-x.jsonl",
        errors_jsonl_path=errors_path,
    )
    await emitter.emit("llm_call", level="error", payload={"url": REMOTE_URL})

    written = errors_path.read_text(encoding="utf-8")
    assert LEAK_GREP.search(written) is None
    assert json.loads(written)["payload"]["url"].startswith("https://«redacted")


async def test_a_failing_errors_sink_does_not_cost_the_main_stream_or_the_caller(
    tmp_path: Path,
) -> None:
    """The errors sink is a FILTER over the main stream (see its field docstring): a caller asking
    whether the event "reached the run's stream" means the main one, so a write failure on the
    side channel must not flip `EmitResult.streamed` to `False` and must not raise. It is still
    counted and retained, under its own sink name, exactly like every other emit failure.
    """
    events_path = tmp_path / "logs" / "events-x.jsonl"
    # A directory where the errors sink expects a plain file: every `open("a")` against it raises.
    bad_errors_path = tmp_path / "logs" / "errors-x.jsonl"
    bad_errors_path.mkdir(parents=True)
    emitter = EventEmitter(run_id=RUN, jsonl_path=events_path, errors_jsonl_path=bad_errors_path)

    result = await emitter.emit("llm_call", level="error", payload={})

    assert result.streamed, "the main sink took the line; the side channel's failure is separate"
    assert events_path.read_text().strip(), "the main stream must not be starved by the side one"
    assert emitter.failed_emits == 1
    assert emitter.failures[-1].sink == "jsonl_errors"
