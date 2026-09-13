"""`fleet.vcs.gitea`: the PR path against a self-hosted Gitea, and the seam that made it possible.

Three properties are worth a test here, and each was a real defect before it was code.

**The token must never enter argv.** `attempts.command` persists argv verbatim and
`WorkerError.stderr_tail` can quote a command line, so `-H "Authorization: token …"` writes a live
credential into `fleet.db` in cleartext. This host already carries 251 mirror remotes with embedded
PATs; §11.4 and `obs/redact.py` exist because that class of leak is the one nobody notices. The
argv test runs with a SYNTHETIC token in a throwaway `curl -K` file, so it never reads the
operator's secret, never needs a network, and never skips.

**`MERGED` must be ingested, not assumed.** SPEC §3.4 step 5: *"`MERGED` is a fact about the forge,
and until something reads it back nothing in this spec ever writes it — which would deadlock the
fleet at the first wave boundary."* Three gates consume it. So the live test MERGES a real PR
through Gitea's API and then asserts the harness's own `sync()` observes `MERGED` — which on Gitea
is not the obvious thing, because a merged PR reports `state: "closed"` and a state-only mapping
would call every landed dependency `CLOSED` and hold its dependents forever.

**Draft is a title on Gitea.** Verified against the live instance rather than assumed: `POST
…/pulls` with `{"draft": true}` and a plain title comes back `"draft": false`, while a `WIP:` title
comes back `"draft": true`. So "open as draft" and "mark ready" are title operations.

The live tests need Gitea at `pr.forge_url` and the operator's `curl -K` file; they skip with the
reason (unreachable, no config, no curl) rather than passing vacuously, and each one cleans up the
throwaway repository it created.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from fleet.models.enums import PrState
from fleet.settings import PrSection
from fleet.util.proc import CommandRunner, ProcResult, run
from fleet.vcs import build_forge
from fleet.vcs import gitea as GT
from fleet.vcs import github as GH
from fleet.vcs.forge import Forge, ForgeError, PrStatus, PrSyncItem

REPO_ROOT = Path(__file__).resolve().parents[1]
CURL_CONFIG = REPO_ROOT / ".secrets" / "gitea-curl.conf"
BASE_URL = "http://localhost:3001"
OWNER = "redmage"

#: Never a real credential: shaped like one so the redactor and the argv scan have something with
#: the right silhouette to find, but valid nowhere.
FAKE_TOKEN = "a0b1c2d3e4f5061728394a5b6c7d8e9f00112233"  # noqa: S105 - a decoy, valid nowhere


# --------------------------------------------------------------------------------------
# a recording runner — command construction is what can be wrong, and it needs no network
# --------------------------------------------------------------------------------------
@dataclass
class RecordingRunner:
    """Injected `CommandRunner` that records argv and replays canned stdout, curl-shaped.

    Replies carry the `\\n%{http_code}` trailer the driver asks curl for, so the transport's own
    status parsing is exercised rather than bypassed.
    """

    replies: list[tuple[int, str]]
    calls: list[tuple[str, ...]]

    def __init__(self, *replies: tuple[int, str]) -> None:
        self.replies = list(replies)
        self.calls = []

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        deadline: float | None = None,
        timeout_s: float | None = None,
    ) -> ProcResult:
        self.calls.append(tuple(argv))
        status, body = self.replies.pop(0) if self.replies else (200, "{}")
        return ProcResult(
            argv=tuple(argv),
            exit_code=0,
            stdout_tail=f"{body}\n{status}",
            stderr_tail="",
            duration_ms=1,
            timed_out=False,
        )


class RaisingRunner:
    """A `CommandRunner` that raises instead of returning a `ProcResult` — what a genuinely
    missing `curl` looks like at this seam. No shell is ever involved, so a binary absent from
    PATH is `FileNotFoundError` raised by `asyncio.create_subprocess_exec` in the PARENT, before
    any process exists to report an exit code — never a `ProcResult` with `exit_code=127`."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        deadline: float | None = None,
        timeout_s: float | None = None,
    ) -> ProcResult:
        raise self.exc


class FixedResultRunner:
    """A `CommandRunner` that replays one exact `ProcResult`, for asserting the two clock-vs-exec
    edge cases `RecordingRunner` cannot build: a never-started call and an ordinary non-zero
    `curl` exit that happens to equal 127."""

    def __init__(self, result: ProcResult) -> None:
        self.result = result

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        deadline: float | None = None,
        timeout_s: float | None = None,
    ) -> ProcResult:
        return self.result


def _fake_config(tmp_path: Path) -> Path:
    """A `curl -K` file in the shape the operator's is: one `header =` line, mode 600."""
    path = tmp_path / "gitea-curl.conf"
    path.write_text(f'header = "Authorization: token {FAKE_TOKEN}"\n', encoding="utf-8")
    path.chmod(0o600)
    return path


def _forge(tmp_path: Path, runner: CommandRunner, **kwargs: Any) -> GT.GiteaForge:
    return GT.GiteaForge(
        owner=OWNER,
        base_url=BASE_URL,
        curl_config=_fake_config(tmp_path),
        repo="monorepo",
        runner=runner,
        request_dir=tmp_path,
        **kwargs,
    )


# --------------------------------------------------------------------------------------
# THE other security property (D22): the credential file's mode is enforced, not documented
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666])
def test_a_group_or_world_readable_credential_file_is_refused(tmp_path: Path, mode: int) -> None:
    """WHY: this module's docstring has promised "the mode-600, gitignored config file" since it
    was written; `chmod`/`st_mode`/`0o600` had zero occurrences anywhere in `src/` before this
    check existed. A group- or world-readable file leaks the API token to any other user on the
    host, silently, forever — refusing to construct the driver is Rule 11's loud failure."""
    path = tmp_path / "gitea-curl.conf"
    path.write_text(f'header = "Authorization: token {FAKE_TOKEN}"\n', encoding="utf-8")
    path.chmod(mode)
    with pytest.raises(GT.GiteaError, match="mode"):
        GT.GiteaForge(owner=OWNER, base_url=BASE_URL, curl_config=path, repo="monorepo")


def test_a_mode_600_credential_file_is_accepted(tmp_path: Path) -> None:
    """WHY: the enforcement above must not become a false-positive refusal of the correct mode —
    every other test in this module depends on `_fake_config`'s 0600 file continuing to work."""
    path = _fake_config(tmp_path)
    GT.GiteaForge(owner=OWNER, base_url=BASE_URL, curl_config=path, repo="monorepo")  # no raise


def test_a_missing_credential_file_is_not_refused_by_the_mode_check(tmp_path: Path) -> None:
    """WHY: existence is `curl`'s own failure at the first request (§ this module's `_exec`), not
    this constructor's job — conflating "missing" with "insecure" is the exact four-state-collapse
    misdiagnosis a later audit of this codebase (D34-D45) found and named."""
    missing = tmp_path / "does-not-exist.conf"
    GT.GiteaForge(owner=OWNER, base_url=BASE_URL, curl_config=missing, repo="monorepo")  # no raise


# --------------------------------------------------------------------------------------
# THE security property: no credential in any argv this harness records
# --------------------------------------------------------------------------------------
def test_the_token_never_appears_in_the_argv_of_a_create_pr_call(tmp_path: Path) -> None:
    """WHY: `attempts.command` persists argv verbatim, so a token on the command line is a
    credential written into `fleet.db` in cleartext — and into `ps` output for every user on the
    box. `curl -K <file>` is what keeps it off the command line: curl reads the header from a
    mode-600 file after `execve`, so the secret exists in no argv, no env var, and no exception.
    """
    created = json.dumps({"html_url": f"{BASE_URL}/{OWNER}/monorepo/pulls/9"})
    runner = RecordingRunner((201, created))
    forge = _forge(tmp_path, runner)
    body_file = tmp_path / "body.md"
    body_file.write_text("## migration\n", encoding="utf-8")

    url = asyncio.run(
        forge.create_pr(
            base="integration",
            head="migrate/acme-billing",
            title="migrate acme",
            body_file=body_file,
        )
    )
    assert url.endswith("/pulls/9")

    assert runner.calls, "the driver executed nothing, so this proves nothing about argv"
    for argv in runner.calls:
        # Never interpolate the token into an assertion message: a failing assert is printed.
        leaked = [index for index, part in enumerate(argv) if FAKE_TOKEN in part]
        assert not leaked, f"the token is in argv element(s) {leaked}; attempts.command keeps argv"
        headers = [index for index, part in enumerate(argv) if "authorization" in part.lower()]
        assert not headers, f"an Authorization header is on argv at {headers}, not in the -K file"
        assert "-K" in argv, "the credential file is not being passed; the token has no other route"


def test_the_request_body_goes_through_a_file_not_argv(tmp_path: Path) -> None:
    """WHY: the same reason `github.py` uses `--body-file`. A PR body quotes build logs and repo
    URLs, and §11.4 keeps those out of the process table and out of any error built from argv.
    A `--data-binary @path` reference is a path; a `-d {json}` would be the payload itself.
    """
    created = json.dumps({"html_url": f"{BASE_URL}/{OWNER}/monorepo/pulls/1"})
    runner = RecordingRunner((201, created))
    forge = _forge(tmp_path, runner)
    body_file = tmp_path / "body.md"
    body_file.write_text("clone https://oauth2:sekrit@git.invalid/x.git failed\n", encoding="utf-8")

    asyncio.run(
        forge.create_pr(base="integration", head="migrate/x", title="t", body_file=body_file)
    )
    argv = runner.calls[0]
    assert any(part.startswith("@") for part in argv), "no `--data-binary @file` reference on argv"
    assert not any("oauth2:sekrit" in part for part in argv), "the body's contents reached argv"


def test_the_request_file_is_deleted_after_the_call(tmp_path: Path) -> None:
    """WHY: the temp file holds the whole PR body. Leaving it behind turns a bounded write into
    an unbounded pile of readable artifacts in `$TMPDIR` across a 250-repo fleet."""
    runner = RecordingRunner((201, json.dumps({"html_url": f"{BASE_URL}/{OWNER}/m/pulls/1"})))
    forge = _forge(tmp_path, runner)
    body_file = tmp_path / "body.md"
    body_file.write_text("x", encoding="utf-8")

    asyncio.run(
        forge.create_pr(base="integration", head="migrate/x", title="t", body_file=body_file)
    )
    assert not list(tmp_path.glob("fleet-gitea-*.json")), "a request body file survived the call"


def test_label_lookup_paginates_past_the_first_page(tmp_path: Path) -> None:
    """WHY: `_label_ids` builds `by_name` from `GET .../labels?limit=100`. A repo with more than
    one page of labels would otherwise silently drop every label past the first 100 from
    `by_name`, misreporting a real label as "does not exist" — exactly the false negative a
    reviewer only discovers by not finding the PR in their filter (this module's own docstring).
    A page of exactly 100 rows must not be mistaken for the last page; the target label sits on
    page 2, so the call only succeeds if the reader actually followed the `page=` parameter.
    """
    page_one = json.dumps([{"id": i, "name": f"l{i}"} for i in range(100)])
    page_two = json.dumps([{"id": 555, "name": "priority"}])
    created = json.dumps({"html_url": f"{BASE_URL}/{OWNER}/monorepo/pulls/42"})
    runner = RecordingRunner((200, page_one), (200, page_two), (201, created))
    forge = _forge(tmp_path, runner)
    body_file = tmp_path / "body.md"
    body_file.write_text("## migration\n", encoding="utf-8")

    url = asyncio.run(
        forge.create_pr(
            base="integration",
            head="migrate/acme-billing",
            title="migrate acme",
            body_file=body_file,
            labels=["priority"],
        )
    )

    assert url.endswith("/pulls/42")
    assert len(runner.calls) == 3, "expected two paginated label GETs, then the create POST"
    assert any("page=1" in part for part in runner.calls[0])
    assert any("page=2" in part for part in runner.calls[1])


# --------------------------------------------------------------------------------------
# pure parsing — the state machine that gates every wave
# --------------------------------------------------------------------------------------
def test_a_merged_pr_is_MERGED_even_though_gitea_calls_its_state_closed() -> None:
    """WHY: this is the deadlock. Gitea reports a merged PR as `state: "closed"`, so the obvious
    state-only mapping classifies every landed dependency as `CLOSED`. `CLOSED` releases none of
    the three gates that consume `MERGED` (Phase 4 stacking, the §3.5 `blocked_by` release, the
    §3.5.1 T1 stub trigger), so the whole fleet parks at the first wave boundary.
    """
    payload = json.dumps(
        {
            "state": "closed",
            "merged": True,
            "merged_at": "2026-08-10T21:46:36Z",
            "merge_commit_sha": "c92c310e4d51efd717827a10a848a8e3abad2bfd",
            "title": "migrate acme",
        }
    )
    status = GT.parse_pr_json("http://h/o/r/pulls/1", payload)
    assert status.state is PrState.MERGED
    assert status.is_merged and status.is_terminal
    assert status.merge_commit_sha == "c92c310e4d51efd717827a10a848a8e3abad2bfd"
    assert status.merged_at is not None and status.merged_at.year == 2026


def test_a_closed_unmerged_pr_is_CLOSED_not_MERGED() -> None:
    """WHY: the mirror image. `merged: false` with `state: closed` is a rejected PR, and calling
    it MERGED would release dependents onto code that never landed."""
    payload = json.dumps({"state": "closed", "merged": False, "title": "t"})
    assert GT.parse_pr_json("http://h/o/r/pulls/1", payload).state is PrState.CLOSED


def test_an_unknown_state_raises_instead_of_defaulting_to_open() -> None:
    """WHY: identical to `parse_pr_view`'s rule. A silently-wrong OPEN is unfalsifiable — it blocks
    every dependent's Phase 4 precondition forever with no error anywhere to find."""
    with pytest.raises(GT.GiteaError, match="unknown PR state"):
        GT.parse_pr_json("http://h/o/r/pulls/1", json.dumps({"state": "frobnicated"}))


def test_non_json_from_the_forge_raises_rather_than_reporting_unchanged() -> None:
    """WHY (Rule 11): an HTML error page parsed as "no change" is how a merged dependency stays
    blocked. A proxy or a logged-out session returns exactly that."""
    with pytest.raises(GT.GiteaError, match="non-JSON"):
        GT.parse_pr_json("http://h/o/r/pulls/1", "<html>502 Bad Gateway</html>")


def test_the_pr_url_is_parsed_by_path_so_gitea_s_ROOT_URL_host_is_irrelevant() -> None:
    """WHY: Gitea stamps `html_url` from its configured `ROOT_URL` — on this box
    `http://192.168.1.12:3001` even when the harness talks to `http://localhost:3001`. A driver
    that rebuilt the API call from the stored URL's host would poll an address the operator never
    configured (and a container-internal hostname would not resolve at all).
    """
    ref = GT.parse_pr_url("http://192.168.1.12:3001/redmage/acme-billing/pulls/42")
    assert (ref.owner, ref.repo, ref.index) == ("redmage", "acme-billing", 42)


def test_a_url_that_is_not_a_pull_request_raises() -> None:
    """WHY: guessing an index polls a DIFFERENT PR and reports another repo's merge state into
    this repo's gate."""
    with pytest.raises(GT.GiteaError, match="not a Gitea pull-request URL"):
        GT.parse_pr_url("http://localhost:3001/redmage/acme-billing/issues/42")


def test_wip_prefixing_is_idempotent() -> None:
    """WHY: a double prefix survives one `mark_ready`, leaving a PR that reads ready and is not."""
    assert GT.draft_title("migrate acme") == "WIP: migrate acme"
    assert GT.draft_title(GT.draft_title("migrate acme")) == "WIP: migrate acme"
    assert GT.strip_wip(GT.draft_title("migrate acme")) == "migrate acme"
    assert GT.strip_wip("[WIP] migrate acme") == "migrate acme"
    assert not GT.is_draft_title("migrate acme")


def test_an_open_wip_pr_reads_as_DRAFTED_and_stays_non_terminal() -> None:
    """WHY: a draft is distinguishable on read, which is what `fleet pr --ready` needs — and
    `DRAFTED` is in `NON_TERMINAL_STATES`, so reporting it changes no gate's behaviour."""
    payload = json.dumps({"state": "open", "merged": False, "title": "WIP: migrate acme"})
    status = GT.parse_pr_json("http://h/o/r/pulls/1", payload)
    assert status.state is PrState.DRAFTED
    assert not status.is_terminal


# --------------------------------------------------------------------------------------
# command construction for the rest of the surface
# --------------------------------------------------------------------------------------
def test_opening_a_draft_prepends_WIP_because_gitea_ignores_a_draft_field(tmp_path: Path) -> None:
    """WHY: verified against Gitea 1.25.4 — `{"draft": true}` on create is ignored and comes back
    `"draft": false`; only a `WIP:` title makes a draft. A driver that sent the field would open
    every `STUB_LIMITED` migration as a READY PR, which §3.4 says a human must clear.
    """
    runner = RecordingRunner((201, json.dumps({"html_url": f"{BASE_URL}/{OWNER}/m/pulls/3"})))
    forge = _forge(tmp_path, runner)
    body_file = tmp_path / "b.md"
    body_file.write_text("b", encoding="utf-8")

    sent: dict[str, Any] = {}

    async def capture() -> None:
        original = forge._write_request  # asserting the payload we send, not the public API

        def spy(body: object) -> Path:
            sent.update(body)  # type: ignore[call-overload]
            return original(body)

        forge._write_request = spy  # type: ignore[method-assign]
        await forge.create_pr(
            base="integration", head="migrate/x", title="migrate acme",
            body_file=body_file, draft=True,
        )

    asyncio.run(capture())
    assert sent["title"] == "WIP: migrate acme"
    assert "draft" not in sent, "a `draft` field would be silently ignored by Gitea"


def test_mark_ready_reads_the_current_title_before_patching_it(tmp_path: Path) -> None:
    """WHY: the title may have been edited on the forge since creation. Reconstructing it from the
    harness's memory would silently revert a human's edit as a side effect of promotion."""
    runner = RecordingRunner(
        (200, json.dumps({"title": "WIP: reviewer renamed this", "state": "open"})),
        (200, json.dumps({"title": "reviewer renamed this", "state": "open"})),
    )
    forge = _forge(tmp_path, runner)
    asyncio.run(forge.mark_ready(f"{BASE_URL}/{OWNER}/m/pulls/4"))

    assert len(runner.calls) == 2, "mark_ready must GET the live title before it PATCHes"
    assert "PATCH" in runner.calls[1]


def test_mark_ready_on_a_ready_pr_is_a_no_op(tmp_path: Path) -> None:
    """WHY: `READY_UNIT` is a re-entrant unit. A retry after a partially-applied promotion must not
    fail, and must not strip a leading word from a title that never had a WIP marker."""
    runner = RecordingRunner((200, json.dumps({"title": "migrate acme", "state": "open"})))
    forge = _forge(tmp_path, runner)
    asyncio.run(forge.mark_ready(f"{BASE_URL}/{OWNER}/m/pulls/5"))
    assert len(runner.calls) == 1, "a non-draft PR must not be PATCHed"


def test_sync_skips_terminal_prs(tmp_path: Path) -> None:
    """WHY: `MERGED` and `CLOSED` cannot change, and a 250-PR fleet re-polling them spends the
    `git_net` semaphore on answers it already has."""
    runner = RecordingRunner((200, json.dumps({"state": "open", "merged": False, "title": "t"})))
    forge = _forge(tmp_path, runner)
    out = asyncio.run(
        forge.sync(
            [
                PrSyncItem(url=f"{BASE_URL}/{OWNER}/m/pulls/1", state=PrState.OPEN),
                PrSyncItem(url=f"{BASE_URL}/{OWNER}/m/pulls/2", state=PrState.MERGED),
                PrSyncItem(url=f"{BASE_URL}/{OWNER}/m/pulls/3", state=PrState.CLOSED),
            ]
        )
    )
    assert len(out) == 1 and len(runner.calls) == 1


def test_an_http_error_is_raised_loudly_with_the_status(tmp_path: Path) -> None:
    """WHY (Rule 11): a 422 ("head branch does not exist") returned as a silent empty result is a
    PR that never opened and a Phase 4 that reports success."""
    runner = RecordingRunner((422, json.dumps({"message": "head branch does not exist"})))
    forge = _forge(tmp_path, runner)
    body_file = tmp_path / "b.md"
    body_file.write_text("b", encoding="utf-8")
    with pytest.raises(GT.GiteaError, match="HTTP 422"):
        asyncio.run(
            forge.create_pr(base="integration", head="migrate/x", title="t", body_file=body_file)
        )


def test_a_rejected_token_is_an_UNAVAILABLE_error_not_a_verdict(tmp_path: Path) -> None:
    """WHY: 401/403 is an operator problem, not a fact about the PR. It is classified apart so a
    poll loop re-queues instead of recording a state, and so the live tests skip on it."""
    runner = RecordingRunner((401, json.dumps({"message": "token does not have scope"})))
    forge = _forge(tmp_path, runner)
    with pytest.raises(GT.GiteaUnavailableError):
        asyncio.run(forge.view(f"{BASE_URL}/{OWNER}/m/pulls/1"))


def test_a_missing_curl_is_named_not_retried_forever(tmp_path: Path) -> None:
    """`curl` absent is an operator-visible condition, not a transient the poll loop should
    retry for 48 hours. `RaisingRunner`, not a scripted `exit_code=127`: no shell is ever
    involved, so a genuinely missing `curl` is `FileNotFoundError` raised in the parent — never
    an exit code on a `ProcResult` that was never produced."""
    runner = RaisingRunner(FileNotFoundError("curl"))
    forge = _forge(tmp_path, runner)
    with pytest.raises(GT.GiteaUnavailableError):
        asyncio.run(forge.view(f"{BASE_URL}/{OWNER}/m/pulls/1"))


def test_curl_exit_127_is_an_ordinary_failure_not_a_missing_binary(tmp_path: Path) -> None:
    """`exit_code == 127` on a returned `ProcResult` used to be read as "curl is missing", but
    nothing in this stack can produce that: no shell means no "command not found" convention. A
    `curl` that RAN and exited 127 of its own accord is an ordinary `GiteaError`."""
    result = ProcResult(
        argv=("curl",), exit_code=127, stdout_tail="", stderr_tail="boom", duration_ms=1,
        timed_out=False, started=True,
    )
    forge = _forge(tmp_path, FixedResultRunner(result))
    with pytest.raises(GT.GiteaError) as exc_info:
        asyncio.run(forge.view(f"{BASE_URL}/{OWNER}/m/pulls/1"))
    assert not isinstance(exc_info.value, GT.GiteaUnavailableError)


def test_a_passed_deadline_is_not_mistaken_for_a_missing_curl(tmp_path: Path) -> None:
    """`not started` is `util.proc.run`'s call-past-deadline synthesis (§7.1) — never a missing
    binary. Misreading it as `GiteaUnavailableError` blames the operator's PATH for the fleet's
    own clock instead of surfacing the retryable clock failure it actually is."""
    result = ProcResult(
        argv=("curl",),
        exit_code=124,
        stdout_tail="",
        stderr_tail="deadline had already passed; process was not started",
        duration_ms=0,
        timed_out=True,
        started=False,
    )
    forge = _forge(tmp_path, FixedResultRunner(result))
    with pytest.raises(GT.GiteaError) as exc_info:
        asyncio.run(forge.view(f"{BASE_URL}/{OWNER}/m/pulls/1"))
    assert not isinstance(exc_info.value, GT.GiteaUnavailableError)
    assert "deadline had already passed" in str(exc_info.value)


# --------------------------------------------------------------------------------------
# the seam: both drivers, one Protocol
# --------------------------------------------------------------------------------------
FORGE_METHODS = ("available", "create_pr", "mark_ready", "edit_body", "view", "sync")


@pytest.mark.parametrize("driver", [GH.GitHubCli, GT.GiteaForge], ids=["github", "gitea"])
def test_both_drivers_satisfy_the_forge_protocol_structurally(driver: type[object]) -> None:
    """WHY: `GitHubCli` is the path 250 repos already depend on; making the forge pluggable must
    not break it. `isinstance` against a `runtime_checkable` Protocol only proves the members
    exist, so the signatures are compared too — a driver that renamed `body_file` would otherwise
    pass the cheap check and fail at the one call site that matters.
    """
    for name in FORGE_METHODS:
        expected = inspect.signature(getattr(Forge, name))
        actual = inspect.signature(getattr(driver, name))
        assert actual.parameters == expected.parameters, f"{driver.__name__}.{name} signature drift"
        assert actual.return_annotation == expected.return_annotation


def test_github_cli_is_still_a_forge_at_runtime() -> None:
    """WHY: the GitHub path must keep working unchanged after the refactor."""
    assert isinstance(GH.GitHubCli(), Forge)


def test_both_drivers_share_one_error_root_so_prwriter_can_catch_one_type() -> None:
    """WHY: `PrwriterWorker` classifies a forge failure as `TRANSIENT_INFRA` from a single
    `except`. Two unrelated exception trees would make the Gitea path fall through as an unhandled
    crash instead of a re-queue."""
    assert issubclass(GH.GhError, ForgeError)
    assert issubclass(GT.GiteaError, ForgeError)


# --------------------------------------------------------------------------------------
# config — an unknown forge is a startup error, not a wave-7 surprise
# --------------------------------------------------------------------------------------
def test_an_unknown_forge_name_is_a_startup_error_naming_the_value() -> None:
    """WHY: the same discipline as an unknown §7.7 backend or an unresolvable §7.4 rewrite engine.
    `pr.forge: gitlab` found at wave 7 is found after 250 repos have been transformed, with the
    fleet parked at a Phase 4 that cannot open a PR."""
    with pytest.raises(ValidationError) as err:
        PrSection(forge="gitlab")
    assert "gitlab" in str(err.value)
    assert "gitea" in str(err.value) and "github" in str(err.value)


def test_gitea_without_a_url_or_owner_is_a_startup_error() -> None:
    """WHY: Gitea's API path IS `/repos/{owner}/{repo}/pulls`. There is nothing to infer, so an
    empty owner is a call that 404s on every repo — better found at load."""
    with pytest.raises(ValidationError, match="forge_url"):
        PrSection(forge="gitea", forge_owner=OWNER)
    with pytest.raises(ValidationError, match="forge_owner"):
        PrSection(forge="gitea", forge_url=BASE_URL)


def test_the_default_forge_is_still_github() -> None:
    """WHY: this change is additive. An existing config with no `pr.forge` key must keep using the
    `gh` path it has been using."""
    assert PrSection().forge == "github"


def test_build_forge_returns_the_named_driver_and_refuses_an_unknown_one(tmp_path: Path) -> None:
    """WHY: the factory is the single place the harness chooses a code host; a `None`-shaped forge
    discovered at the first PR is the failure this raise replaces."""
    assert isinstance(build_forge("github"), GH.GitHubCli)
    assert isinstance(
        build_forge("gitea", base_url=BASE_URL, owner=OWNER, curl_config=_fake_config(tmp_path)),
        GT.GiteaForge,
    )
    with pytest.raises(ForgeError, match="gitlab"):
        build_forge("gitlab")
    with pytest.raises(ForgeError, match="forge_token_config"):
        build_forge("gitea", base_url=BASE_URL, owner=OWNER)


# --------------------------------------------------------------------------------------
# LIVE — against the operator's Gitea. Skips loudly, cleans up after itself.
# --------------------------------------------------------------------------------------
def _api(method: str, path: str, body: object | None = None) -> tuple[int, Any]:
    """Test scaffolding: talk to Gitea directly, independent of the driver under test.

    Deliberately not `GiteaForge` — a test that set up its fixtures with the code under test would
    report a green run when both the setup and the assertion were wrong in the same way.
    """
    argv = [
        "curl", "-sS", "-K", str(CURL_CONFIG), "-X", method,
        "-H", "Accept: application/json", "-w", "\n%{http_code}",
    ]
    if body is not None:
        argv += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    argv.append(f"{BASE_URL}/api/v1{path}")
    result = asyncio.run(run(argv, timeout_s=30))
    if not result.ok:
        return 0, result.stderr_tail
    text, _, code = result.stdout_tail.rstrip().rpartition("\n")
    return int(code), (json.loads(text) if text.strip() else None)


def _skip_reason() -> str | None:
    if shutil.which("curl") is None:
        return "curl is not installed; the Gitea driver shells curl so it can pass -K"
    if not CURL_CONFIG.exists():
        return f"no credential file at {CURL_CONFIG} (mode-600 `curl -K` file with the API token)"
    status, payload = _api("GET", "/user")
    if status == 0:
        return f"Gitea at {BASE_URL} is unreachable: {payload}"
    if status != 200:
        return f"Gitea at {BASE_URL} rejected the credential file (HTTP {status})"
    return None


SKIP = _skip_reason()
live = pytest.mark.skipif(SKIP is not None, reason=SKIP or "")


@pytest.fixture(scope="module")
def live_repo() -> Any:
    """A throwaway Gitea repository with two migration branches, deleted on teardown.

    Sync + `asyncio.run` on purpose: a module-scoped async fixture would need its own event-loop
    scope, and the setup is HTTP, not something under test.
    """
    name = f"fleet-gitea-test-{uuid4().hex[:8]}"
    status, _ = _api(
        "POST", "/user/repos",
        {"name": name, "auto_init": True, "default_branch": "main", "private": True},
    )
    assert status == 201, f"could not create the throwaway repo (HTTP {status})"
    try:
        for branch in ("migrate/one", "migrate/two"):
            slug = branch.replace("/", "-")
            code, _ = _api(
                "POST", f"/repos/{OWNER}/{name}/contents/{slug}.txt",
                {"content": "aGVsbG8=", "message": slug, "branch": "main", "new_branch": branch},
            )
            assert code == 201, f"could not seed {branch} (HTTP {code})"
        yield name
    finally:
        _api("DELETE", f"/repos/{OWNER}/{name}")


def _live_forge(name: str) -> GT.GiteaForge:
    return GT.GiteaForge(
        owner=OWNER, base_url=BASE_URL, curl_config=CURL_CONFIG, repo=name
    )


@live
def test_live_available_reports_true_against_a_reachable_authenticated_gitea() -> None:
    """WHY: `available()` is the gate the CLI and these tests trust. One that returned True while
    unauthenticated would make every later call fail with an error a poll loop retries forever."""
    assert asyncio.run(_live_forge("unused").available()) is True


@live
def test_live_a_real_pr_is_created_and_reads_back_as_the_harness_expects(
    live_repo: str, tmp_path: Path
) -> None:
    """WHY: everything above is construction and parsing. This is the one that proves the two
    halves meet a real Gitea 1.25.4: `create_pr` opens a PR the server accepts, and `view` on the
    URL it returned reports `OPEN` with no merge commit — the state Phase 4 records at emission.
    """
    body_file = tmp_path / "body.md"
    body_file.write_text("## Migration of `acme/billing`\n", encoding="utf-8")
    forge = _live_forge(live_repo)

    url = asyncio.run(
        forge.create_pr(
            base="main", head="migrate/one", title="migrate acme/billing", body_file=body_file
        )
    )
    assert "/pulls/" in url

    status: PrStatus = asyncio.run(forge.view(url))
    assert status.state is PrState.OPEN
    assert not status.is_merged and not status.is_terminal
    assert status.merge_commit_sha is None

    # The body reached the forge through the file, intact.
    ref = GT.parse_pr_url(url)
    code, payload = _api("GET", f"/repos/{OWNER}/{live_repo}/pulls/{ref.index}")
    assert code == 200 and "Migration of" in payload["body"]


@live
def test_live_merging_a_pr_is_ingested_as_MERGED_by_the_harness_sync_path(
    live_repo: str, tmp_path: Path
) -> None:
    """WHY: THE deadlock (§3.4 step 5). `MERGED` is read by three gates and written by nothing
    unless a sync observes it. Gitea reports a merged PR as `state: "closed"`, so this merges a
    real PR through the API and then asserts the harness's own `sync()` — the exact call
    `PrwriterWorker._sync` and `fleet pr --sync` make — returns `MERGED` with the merge commit
    Phase 4 records, rather than the `CLOSED` a state-only reading would produce.
    """
    body_file = tmp_path / "body.md"
    body_file.write_text("dependency PR\n", encoding="utf-8")
    forge = _live_forge(live_repo)

    url = asyncio.run(
        forge.create_pr(base="main", head="migrate/two", title="dependency", body_file=body_file)
    )
    ref = GT.parse_pr_url(url)

    before = asyncio.run(forge.sync([PrSyncItem(url=url, state=PrState.OPEN)]))
    assert before[0].state is PrState.OPEN

    code, _ = _api("POST", f"/repos/{OWNER}/{live_repo}/pulls/{ref.index}/merge", {"Do": "merge"})
    assert code == 200, f"the scaffolding could not merge the PR (HTTP {code})"

    after = asyncio.run(forge.sync([PrSyncItem(url=url, state=PrState.OPEN)]))
    assert len(after) == 1
    assert after[0].state is PrState.MERGED, "a merged Gitea PR reports state='closed'"
    assert after[0].is_merged and after[0].is_terminal
    assert after[0].merge_commit_sha, "a MERGED state with no commit is a partial answer"
    assert after[0].merged_at is not None

    # And a terminal PR is no longer polled — the semaphore is not spent on a fixed answer.
    assert asyncio.run(forge.sync([PrSyncItem(url=url, state=PrState.MERGED)])) == ()


@live
def test_live_a_draft_pr_is_distinguishable_and_mark_ready_promotes_it(
    live_repo: str, tmp_path: Path
) -> None:
    """WHY: §3.4 makes draft a verdict — a `STUB_LIMITED` migration MUST open as one, and a human
    clears it. Gitea has no `draft` field to set, so this proves the title mechanism end to end:
    the server itself reports `draft: true` for what the driver opened, `view` reports `DRAFTED`,
    and `mark_ready` flips both back.
    """
    body_file = tmp_path / "body.md"
    body_file.write_text("stub-limited\n", encoding="utf-8")
    forge = _live_forge(live_repo)

    # Its own branch, so this test is independent of the merge test's ordering.
    branch = "migrate/draft"
    code, _ = _api(
        "POST", f"/repos/{OWNER}/{live_repo}/contents/draft.txt",
        {"content": "aGk=", "message": "draft", "branch": "main", "new_branch": branch},
    )
    assert code == 201

    url = asyncio.run(
        forge.create_pr(
            base="main", head=branch, title="migrate stubbed", body_file=body_file, draft=True
        )
    )
    ref = GT.parse_pr_url(url)

    _, payload = _api("GET", f"/repos/{OWNER}/{live_repo}/pulls/{ref.index}")
    assert payload["draft"] is True, "Gitea did not treat the WIP title as a draft"
    assert payload["title"].startswith("WIP: ")
    assert asyncio.run(forge.view(url)).state is PrState.DRAFTED

    asyncio.run(forge.mark_ready(url))

    _, promoted = _api("GET", f"/repos/{OWNER}/{live_repo}/pulls/{ref.index}")
    assert promoted["draft"] is False
    assert promoted["title"] == "migrate stubbed"
    assert asyncio.run(forge.view(url)).state is PrState.OPEN


@live
def test_live_a_draft_field_on_create_would_have_been_ignored(live_repo: str) -> None:
    """WHY: this is the fact the whole draft implementation rests on, and it is a property of the
    server, not of this code. Asserted against the live API so a Gitea upgrade that starts honouring
    `draft` fails HERE — with a one-line fix — rather than silently opening ready PRs for months.
    """
    branch = "migrate/draftfield"
    code, _ = _api(
        "POST", f"/repos/{OWNER}/{live_repo}/contents/df.txt",
        {"content": "aGk=", "message": "df", "branch": "main", "new_branch": branch},
    )
    assert code == 201
    status, payload = _api(
        "POST", f"/repos/{OWNER}/{live_repo}/pulls",
        {"head": branch, "base": "main", "title": "plain title", "body": "b", "draft": True},
    )
    assert status == 201
    assert payload["draft"] is False, (
        "Gitea now honours a `draft` field on create; GiteaForge.create_pr may stop prepending WIP:"
    )
