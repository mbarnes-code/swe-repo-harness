"""SPEC §12 item 20's remaining residual (`docs/CRITERIA_PLAN.md` §20's "Done bar (remaining)"):
*"the generated PR body contains the `«redacted:…»` placeholder rather than the value"* — the one
clause of the secrets-never-leak criterion left entirely untested after D88/D90 closed the other
three named DB columns (`events.payload`, `attempts.stdout_tail`/`stderr_tail`,
`phases.last_error`).

This follows the same pattern those two defects established
(`tests/test_repository.py::test_record_attempt_redacts_a_credential_in_stdout_and_stderr_tail_before_the_write`):
plant a credential-shaped secret through a real, production write path, and read the *persisted*
value back — never a string match against source.

**Where the secret actually flows.** `workers/prwriter.py::render_body` renders
`payload.relocation_summary` verbatim, one bullet per entry (`"### Relocation map"`), from data a
worker plan can legitimately populate with a URL or log excerpt. `render_body` itself does not
redact — by design, since its output also becomes `body_file` for the real `gh`/Gitea PR (a
credential-free field per `PrwriterInput.source_url`'s own docstring, so nothing upstream should
be planting one there in production). The redaction boundary is `cli.py::_write_pr_record`, the
sole writer of the `PullRequest` `findings` row (`PR_RECORD_KIND`), which calls
`redact_text(draft.model_dump_json())` before the INSERT — the same `redact_text` call D88/D90
added to the sibling DB columns. This test drives both: the real `render_body` construction and
the real `_write_pr_record` persistence.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import aiosqlite
import pytest

from fleet.cli import PR_RECORD_KIND, _write_pr_record
from fleet.models.enums import Equivalence, PrState
from fleet.models.tasks import PullRequestDraft, VerificationReport
from fleet.state.db import StateWriter, connect_ro, initialize_database
from fleet.state.repository import SqliteStateRepository
from fleet.workers.prwriter import PrwriterInput, render_body

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
RUN = "33333333-3333-4333-8333-333333333333"
REPO = "acme-widgets"
PAT = "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"


def _report() -> VerificationReport:
    return VerificationReport(
        run_id=UUID(RUN),
        repo_id=REPO,
        build_ok=True,
        test_ok=True,
        rdeps_ok=True,
        rdeps_target_count=1,
        rdeps_tested=1,
        verdict="PASS",
    )


def _payload(**overrides: object) -> PrwriterInput:
    fields: dict[str, object] = {
        "report": _report(),
        "wave_index": 0,
        "branch": f"migrate/{REPO}",
        "source_url": "https://example.invalid/acme/widgets.git",
        "source_sha": "f" * 40,
    }
    fields.update(overrides)
    return PrwriterInput(**fields)  # type: ignore[arg-type]


def _draft(*, body: str) -> PullRequestDraft:
    return PullRequestDraft(
        run_id=UUID(RUN),
        repo_id=REPO,
        wave_index=0,
        branch=f"migrate/{REPO}",
        title="[fleet wave 0] migrate acme-widgets into the monorepo",
        body=body,
        source_url="https://example.invalid/acme/widgets.git",
        source_sha="f" * 40,
        state=PrState.DRAFTED,
        equivalence=Equivalence.FULL,
    )


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "fleet.db"
    await initialize_database(path)
    return path


@pytest.fixture
async def writer(db_path: Path) -> AsyncIterator[StateWriter]:
    """A `runs`/`repos` row for the FK the `findings` INSERT needs, seeded the same way
    `tests/test_repository.py::repo` does — through the real `upsert_run`/`upsert_repo` writer
    methods, not hand-rolled SQL that could drift from the schema."""
    async with StateWriter(db_path, owner="test-writer") as w:
        read_conn = await connect_ro(db_path)
        try:
            store = SqliteStateRepository(writer=w, read_conn=read_conn)
            await store.upsert_run(
                RUN, started_at=NOW, config_sha256="a" * 64, harness_version="0.1.0"
            )
            await store.upsert_repo(
                REPO, name=REPO, url="https://example.invalid/acme/widgets.git", now=NOW
            )
        finally:
            await read_conn.close()
        yield w


async def _persisted_payload(db_path: Path) -> str:
    conn: aiosqlite.Connection = await connect_ro(db_path)
    try:
        cur = await conn.execute(
            "SELECT payload FROM findings WHERE run_id = ? AND kind = ?",
            (RUN, PR_RECORD_KIND),
        )
        row = await cur.fetchone()
    finally:
        await conn.close()
    assert row is not None, "the PR record never landed"
    return str(row[0])


async def test_write_pr_record_redacts_a_credential_that_reached_the_pr_body(
    writer: StateWriter, db_path: Path
) -> None:
    """Criterion-closure test (§12.20, SECURITY-RELEVANT): the PR-body `«redacted:…»` placeholder
    clause, confirmed untested by two prior rounds' investigations (`docs/CRITERIA_PLAN.md` §20)
    but not a live defect — `_write_pr_record` already redacted correctly; this test closes the
    coverage gap, it does not disclose a new one. A credential-shaped secret planted in
    `relocation_summary` — real content a transform worker can legitimately hand `PrwriterInput`,
    e.g. a relocation note quoting the mirror URL it moved a path from — reaches `render_body`'s
    rendered PR body verbatim (`render_body` never redacts: its output is also the `gh`/Gitea
    `body_file`). The persistence boundary is `cli.py::_write_pr_record`, which strips it before
    the `findings` INSERT.

    The control is the test below: an innocuous body must survive unredacted.
    """
    tainted_note = f"relocated from https://oauth2:{PAT}@gitea.local:3001/acme/widgets.git"
    payload = _payload(relocation_summary=[tainted_note])
    body = render_body(payload, repo_id=REPO, draft=True)
    assert PAT in body, "the fixture is broken: the secret never reached the rendered body"

    await _write_pr_record(writer, RUN, _draft(body=body), now=NOW)

    persisted = await _persisted_payload(db_path)
    assert PAT not in persisted, f"a live PAT reached the persisted PR record: {persisted!r}"
    assert "github_pat_" not in persisted
    assert "«redacted:" in persisted, "the placeholder must survive, or debugging is blind"
    assert "gitea.local" in persisted, "over-redaction destroyed the debuggable part too"


async def test_write_pr_record_leaves_an_innocuous_pr_body_unchanged(
    writer: StateWriter, db_path: Path
) -> None:
    """The control for the test above: this fix is not free to over-redact its way to green."""
    payload = _payload(relocation_summary=["relocated from libs/old/path to libs/new/path"])
    body = render_body(payload, repo_id=REPO, draft=True)

    await _write_pr_record(writer, RUN, _draft(body=body), now=NOW)

    persisted = await _persisted_payload(db_path)
    assert "libs/old/path" in persisted and "libs/new/path" in persisted
    assert "«redacted:" not in persisted
