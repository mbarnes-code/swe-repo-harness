"""Gitea driver for the §3.4 PR path — create, mark-ready, and the `MERGED` state ingestion.

`gh` speaks the GitHub API and nothing else, so pointing it at a self-hosted Gitea is not a
base-URL swap; it is a different protocol. This module is the second `vcs/forge.Forge`
implementation, verified against a live **Gitea 1.25.4**:

* create — `POST /api/v1/repos/{owner}/{repo}/pulls` with `{head, base, title, body}`;
* read — `GET  /api/v1/repos/{owner}/{repo}/pulls/{index}`, which carries `state`, `merged`,
  `merged_at` and `merge_commit_sha`;
* auth — `Authorization: token <TOKEN>`.

**A merged PR reports `state: "closed"`.** This is the single most important difference from
GitHub, and reading `state` alone would silently classify every merged dependency as `CLOSED`:
three gates consume `PrState.MERGED` (the Phase 4 stacking precondition, the §3.5 `blocked_by`
release, the §3.5.1 T1 stub trigger) and a `CLOSED` dependency releases none of them. So
`parse_pr_json` reads the boolean `merged` FIRST and only then falls back to `state`.

**Draft is a TITLE, not a field.** Confirmed against the live instance: `POST …/pulls` with
`{"draft": true}` and a plain title returns `"draft": false` — the flag is ignored — whereas a
title beginning `WIP:` returns `"draft": true`. The response's `draft` key is derived from the
title, so opening a draft prepends `WIP: ` and `mark_ready` PATCHes the prefix back off. That also
makes an open draft distinguishable on read, which is why `view()` reports `DRAFTED` (non-terminal,
so no gate changes) rather than flattening it to `OPEN`.

**THE TOKEN NEVER TOUCHES argv.** `attempts.command` persists argv verbatim and
`WorkerError.stderr_tail` can quote a command line, so `-H "Authorization: token …"` would write a
live credential into the state DB — the exact failure `obs/redact.py` exists for, on a host that
already carries mirror remotes with embedded PATs. Every request is therefore
`curl -K <config-file>`, where the mode-600, gitignored config file holds the `header = "…"` line.
The token is read by curl, from disk, after `execve`; it is in no argv, no environment variable and
no exception message. `tests/test_gitea.py` asserts exactly that over a constructed create-PR argv.

**Request bodies go through a file too**, for the same reason `github.py` uses `--body-file`: a PR
body quotes build logs and repo URLs, and `--data-binary @file` keeps them out of the process table.

Everything goes through the injected `CommandRunner` seam, so command construction, URL parsing and
state mapping are all verifiable with no Gitea and no network; `available()` is the honest gate for
the tests that genuinely need the server.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from fleet.models.enums import PrState
from fleet.obs.redact import redact_text
from fleet.util.proc import CommandRunner, run
from fleet.vcs.forge import (
    NON_TERMINAL_STATES,
    ForgeError,
    ForgeUnavailableError,
    PrStatus,
    PrSyncItem,
)
from fleet.vcs.git import redact_argv

__all__ = [
    "API_PREFIX",
    "WIP_PREFIX",
    "WIP_PREFIXES",
    "GiteaError",
    "GiteaForge",
    "GiteaUnavailableError",
    "PrRef",
    "draft_title",
    "is_draft_title",
    "parse_pr_json",
    "parse_pr_url",
    "strip_wip",
]

API_PREFIX: Final = "/api/v1"

WIP_PREFIX: Final = "WIP: "
"""What this driver PREPENDS to open a draft. Gitea's own default WIP marker."""

WIP_PREFIXES: Final[tuple[str, ...]] = ("WIP:", "[WIP]", "WIP")
"""What `mark_ready` STRIPS. Wider than what we write, because `repository.pull-request.WORK_IN_
PROGRESS_PREFIXES` is operator-configurable and a PR opened by a human may carry either form.
Ordered longest-marker-first so `[WIP]` is not left as `]` by the bare `WIP` rule."""

_STATE_MAP: Final[Mapping[str, PrState]] = {"open": PrState.OPEN, "closed": PrState.CLOSED}

_CURL_CONNECT_FAILURES: Final[frozenset[int]] = frozenset({6, 7, 28, 35})
"""curl's "the server is not there" exits: unresolved host, connection refused, timeout, TLS
handshake. These are `GiteaUnavailableError` (a re-queue, and what a test skips on), never a
verdict about the PR."""


class GiteaError(ForgeError):
    """A Gitea call failed or returned something unparseable. Loud (Rule 11)."""


class GiteaUnavailableError(GiteaError, ForgeUnavailableError):
    """`curl` is missing, or the Gitea instance is unreachable/unauthenticated."""


def _require_private_credential_file(path: Path) -> None:
    """Refuse a `curl -K` credential file that group or other can read (§11.4, D22).

    This module's own docstring has promised *"the mode-600, gitignored config file"* since it was
    written, and nothing ever checked it — `chmod`, `st_mode` and `0o600` had zero occurrences
    anywhere in `src/` before this. A world-readable token file works silently right up until
    another user on the same host reads the Gitea API token straight off disk, so this fails loud
    at construction (Rule 11) instead of only in prose.

    Existence is deliberately not checked here: a missing file is `curl`'s own, already-surfaced
    failure (`-K`: no such file, or `FileNotFoundError` if `curl` itself is absent) at the first
    request. This function's only job is the permission bits of a file that IS there — conflating
    "missing" with "insecure" would misdiagnose an operator who simply has not created it yet.
    """
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & 0o077:
        raise GiteaError(
            f"{path} is readable by group or other (mode {oct(mode & 0o777)}); refusing to use "
            "it as the `curl -K` credential file for the Gitea API token. `chmod 600` it first — "
            "any other user on this host can otherwise read the token straight off disk (§11.4)"
        )


@dataclass(frozen=True, slots=True)
class PrRef:
    """The `{owner}/{repo}#{index}` triple an API path needs, recovered from a PR's web URL."""

    owner: str
    repo: str
    index: int


def parse_pr_url(url: str) -> PrRef:
    """`http://host:3001/redmage/proj/pulls/7` → `PrRef("redmage", "proj", 7)`.

    Only the PATH is used. Gitea stamps `html_url` from its configured `ROOT_URL`, which on this
    operator's box is `http://192.168.1.12:3001` even when the harness talks to
    `http://localhost:3001` — so a driver that rebuilt the API call from the stored URL's host
    would poll an address the operator never configured, and a container-internal hostname would
    not resolve at all. The configured `base_url` always wins; the URL supplies identity only.
    """
    parts = [segment for segment in urlsplit(url).path.split("/") if segment]
    if len(parts) < 4 or parts[-2] not in ("pulls", "pull"):
        raise GiteaError(
            f"{url!r} is not a Gitea pull-request URL (expected .../{{owner}}/{{repo}}/pulls/"
            "{index}); refusing to guess an index, because polling the wrong PR reports another "
            "repo's merge state into this repo's gate (§3.4 step 5)"
        )
    try:
        index = int(parts[-1])
    except ValueError as exc:
        raise GiteaError(f"{url!r} has a non-numeric pull-request index {parts[-1]!r}") from exc
    return PrRef(owner=parts[-4], repo=parts[-3], index=index)


def is_draft_title(title: str) -> bool:
    """Gitea's own rule: a draft is a title carrying a WIP marker (case-insensitive)."""
    lowered = title.lstrip().lower()
    return any(lowered.startswith(prefix.lower()) for prefix in WIP_PREFIXES)


def strip_wip(title: str) -> str:
    """Remove the WIP marker — the whole of "mark ready" on Gitea."""
    text = title.lstrip()
    for prefix in WIP_PREFIXES:
        if text.lower().startswith(prefix.lower()):
            return text[len(prefix) :].lstrip()
    return text


def draft_title(title: str) -> str:
    """Prepend the WIP marker, idempotently: double-prefixing would survive one `mark_ready` and
    leave a PR that reads ready but is still a draft."""
    return title if is_draft_title(title) else f"{WIP_PREFIX}{title.lstrip()}"


def _parse_timestamp(raw: object) -> datetime | None:
    """Gitea emits RFC3339 with a `Z`; an unmerged PR emits `null`."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GiteaError(f"unparseable merged_at from Gitea: {raw!r}") from exc


def parse_pr_json(url: str, payload: str) -> PrStatus:
    """Parse one `GET …/pulls/{index}` body into a `PrStatus`. Pure, and the whole state machine.

    Tested against recorded Gitea JSON as well as against the live instance, because this is the
    function that decides whether a wave advances. Two rules earn their lines:

    * `merged` is read BEFORE `state`. A merged Gitea PR reports `state: "closed"`, so the obvious
      state-only mapping would call every landed dependency `CLOSED` and hold its dependents
      forever.
    * an unknown `state` RAISES rather than defaulting to OPEN, exactly as `parse_pr_view` does: a
      silently-wrong OPEN blocks every dependent's Phase 4 precondition with no error to find.
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise GiteaError(
            f"Gitea returned non-JSON for {url}: {redact_text(payload)[:200]!r}"
        ) from exc
    if not isinstance(data, dict):
        raise GiteaError(f"Gitea returned {type(data).__name__}, expected an object, for {url}")

    merged_at = _parse_timestamp(data.get("merged_at"))
    sha = data.get("merge_commit_sha")
    merge_commit_sha = sha if isinstance(sha, str) and sha else None

    if data.get("merged") is True:
        state = PrState.MERGED
    else:
        raw_state = data.get("state")
        if not isinstance(raw_state, str) or raw_state.lower() not in _STATE_MAP:
            raise GiteaError(
                f"Gitea reported an unknown PR state {raw_state!r} for {url}; refusing to guess, "
                "because an assumed OPEN blocks every dependent's Phase 4 precondition (§3.4 "
                "step 5)"
            )
        state = _STATE_MAP[raw_state.lower()]
        title = data.get("title")
        if state is PrState.OPEN and isinstance(title, str) and is_draft_title(title):
            # Derived from the title, not from the response's `draft` key: the key is Gitea's own
            # derivation of the same thing, and reading the title keeps this function honest
            # against the exact bytes `create_pr` wrote.
            state = PrState.DRAFTED
    return PrStatus(
        url=url, state=state, merged_at=merged_at, merge_commit_sha=merge_commit_sha
    )


class GiteaForge:
    """The Gitea surface the harness uses, over the injected `CommandRunner` (guardrail 3).

    `curl` rather than `urllib`: every other external boundary in this harness is a subprocess
    behind `util.proc.run`, which is what gives the tests a recording fake, the deadline the
    process-group kill, and the tails their §11.4 redaction. A `urllib` call would need a second,
    parallel seam and would add an unbounded in-memory read — and `curl -K` is also the mechanism
    that keeps the token out of argv.
    """

    def __init__(
        self,
        *,
        owner: str,
        base_url: str,
        curl_config: Path,
        repo: str | None = None,
        runner: CommandRunner = run,
        curl_bin: str = "curl",
        cwd: Path | None = None,
        deadline: float | None = None,
        timeout_s: float | None = 120.0,
        request_dir: Path | None = None,
    ) -> None:
        self._owner = owner
        self._api = f"{base_url.rstrip('/')}{API_PREFIX}"
        self._curl_config = Path(curl_config)
        _require_private_credential_file(self._curl_config)
        self._repo = repo
        self._runner = runner
        self._curl = curl_bin
        self.cwd = cwd
        self.deadline = deadline
        self.timeout_s = timeout_s
        self._request_dir = request_dir

    # ------------------------------------------------------------------ argv construction

    def argv(self, args: Sequence[str]) -> tuple[str, ...]:
        """The full command line. `-K <file>` is the ONLY place credentials enter the process, and
        a test asserts the token appears in no element of what this returns."""
        return (self._curl, "-sS", "-K", str(self._curl_config), *args)

    def _request_argv(self, method: str, url: str, *, data_file: Path | None) -> tuple[str, ...]:
        args = [
            "-X",
            method,
            "-H",
            "Accept: application/json",
            # `\n%{http_code}` rather than `--fail`: the status is needed IN the error message, and
            # the body of a 4xx is Gitea's own explanation of what was wrong with the request.
            "-w",
            "\n%{http_code}",
        ]
        if data_file is not None:
            args += ["-H", "Content-Type: application/json", "--data-binary", f"@{data_file}"]
        args.append(url)
        return self.argv(args)

    # ------------------------------------------------------------------ transport

    async def _exec(self, method: str, path: str, *, body: object | None = None) -> str:
        """One API call → the response body. Raises loudly on transport or HTTP failure."""
        url = f"{self._api}{path}"
        data_file: Path | None = None
        try:
            if body is not None:
                data_file = self._write_request(body)
            parts = self._request_argv(method, url, data_file=data_file)
            # No shell is ever involved (`util/proc.py`'s module docstring), so `curl` missing
            # from PATH never comes back as a `ProcResult` with some sentinel exit code — there
            # is no shell to apply the "command not found: exit 127" convention. It is
            # `asyncio.create_subprocess_exec` raising `FileNotFoundError` in THIS process,
            # before any `ProcResult` exists, and it is caught here or it escapes raw.
            try:
                result = await self._runner(
                    parts, cwd=self.cwd, deadline=self.deadline, timeout_s=self.timeout_s
                )
            except FileNotFoundError as exc:
                raise GiteaUnavailableError(
                    f"{self._curl} is not on PATH; PR emission and PR state ingestion both "
                    f"require it (§3.4): {exc}"
                ) from exc
        finally:
            if data_file is not None:
                data_file.unlink(missing_ok=True)

        # `result.started` is false in exactly one case — `util.proc.run` synthesised a
        # deadline that had already passed (§7.1) — never a missing binary; that case already
        # fails `result.ok` below and is reported as the clock failure it is.
        if result.exit_code in _CURL_CONNECT_FAILURES:
            raise GiteaUnavailableError(
                f"cannot reach the Gitea instance at {self._api} (curl exit {result.exit_code}): "
                f"{result.stderr_tail}"
            )
        if not result.ok:
            raise GiteaError(
                f"{' '.join(redact_argv(parts)[1:])} failed (exit {result.exit_code}): "
                f"{result.stderr_tail}"
            )

        status, payload = _split_status(result.stdout_tail)
        if status in (401, 403):
            raise GiteaUnavailableError(
                f"Gitea rejected the credentials in {self._curl_config} for {method} {path} "
                f"(HTTP {status}): {redact_text(payload)[:200]}"
            )
        if not 200 <= status < 300:
            raise GiteaError(
                f"Gitea {method} {path} returned HTTP {status}: {redact_text(payload)[:400]}"
            )
        return payload

    def _write_request(self, body: object) -> Path:
        """Serialise a JSON request body to a mode-600 temp file for `--data-binary @…`.

        Not argv, for the same reason `github.py` uses `--body-file`: a PR body quotes build logs
        and repo URLs, and §11.4 keeps those out of the process table and out of any error message
        built from argv. `mkstemp` creates the file 0600 before anything is written to it.
        """
        handle, name = tempfile.mkstemp(
            prefix="fleet-gitea-", suffix=".json", dir=self._request_dir
        )
        with os.fdopen(handle, "w", encoding="utf-8") as sink:
            json.dump(body, sink)
        return Path(name)

    # ------------------------------------------------------------------ the Forge surface

    async def available(self) -> bool:
        """Is Gitea reachable AND is the configured token accepted? Both, because an unauthorised
        token fails every later call with an error a poll loop would otherwise retry forever."""
        try:
            await self._exec("GET", "/user")
        except GiteaError:
            return False
        return True

    async def create_pr(
        self,
        *,
        base: str,
        head: str,
        title: str,
        body_file: Path,
        draft: bool = False,
        repo: str | None = None,
        labels: Sequence[str] = (),
    ) -> str:
        """`POST /repos/{owner}/{repo}/pulls` → the PR's web URL.

        `draft=True` prepends `WIP: ` because that — not a `draft` field — is how Gitea marks a
        draft; the API ignores a `draft` key on create (verified against 1.25.4).
        """
        owner, name = self._slug(repo)
        payload: dict[str, object] = {
            "head": head,
            "base": base,
            "title": draft_title(title) if draft else title,
            "body": _read_body(body_file),
        }
        if labels:
            payload["labels"] = await self._label_ids(owner, name, labels)
        data = _as_object(await self._exec("POST", f"/repos/{owner}/{name}/pulls", body=payload))
        url = data.get("html_url") or data.get("url")
        if not isinstance(url, str) or not url:
            raise GiteaError(
                f"Gitea created a PR in {owner}/{name} but returned no URL; Phase 4's success "
                "criterion is a RESOLVABLE pr url, so this is a failure, not a silent None"
            )
        return url

    async def mark_ready(self, url: str) -> None:
        """Promotion out of draft = removing the WIP prefix (`PATCH …/pulls/{index}`).

        Reads the current title first rather than reconstructing it: the title may have been edited
        on the forge since creation, and PATCHing a stale title would silently revert a human's
        edit. A PR that is not a draft is left alone — `mark_ready` is idempotent, because a retry
        after a partially-applied `READY_UNIT` must not fail.
        """
        ref = parse_pr_url(url)
        path = f"/repos/{ref.owner}/{ref.repo}/pulls/{ref.index}"
        current = _as_object(await self._exec("GET", path))
        title = current.get("title")
        if not isinstance(title, str):
            raise GiteaError(f"Gitea returned no title for {url}; cannot clear its WIP prefix")
        if not is_draft_title(title):
            return
        await self._exec("PATCH", path, body={"title": strip_wip(title)})

    async def edit_body(self, url: str, body_file: Path, *, title: str | None = None) -> None:
        """`PATCH /repos/{owner}/{repo}/pulls/{index}` with a new `body` (and `title`, if given).

        Mirrors `github.py`'s `edit_body`: §3.5.1's resolution re-uses the same PR rather than
        opening a second one, so its number, url and review history survive a revalidation round.
        Unlike `mark_ready`, this never reads the current title first — a caller not passing
        `title` leaves it untouched, so there is nothing stale to guard against.
        """
        ref = parse_pr_url(url)
        path = f"/repos/{ref.owner}/{ref.repo}/pulls/{ref.index}"
        body: dict[str, object] = {"body": _read_body(body_file)}
        if title is not None:
            body["title"] = title
        await self._exec("PATCH", path, body=body)

    async def view(self, url: str) -> PrStatus:
        """`GET /repos/{owner}/{repo}/pulls/{index}` → `PrStatus`. The only source of `MERGED`."""
        ref = parse_pr_url(url)
        payload = await self._exec("GET", f"/repos/{ref.owner}/{ref.repo}/pulls/{ref.index}")
        return parse_pr_json(url, payload)

    async def sync(self, prs: Iterable[PrSyncItem]) -> tuple[PrStatus, ...]:
        """Poll every NON-TERMINAL PR and return what Gitea says (`fleet pr --sync`).

        Sequential for the same reason the GitHub driver is: the poll is bounded by the `git_net`
        semaphore (§3.4), and a single-box Gitea handed 250 concurrent requests turns a status read
        into an outage. Results are returned for the single writer (§11.5); nothing here writes.
        """
        out: list[PrStatus] = []
        for item in prs:
            if item.state in NON_TERMINAL_STATES:
                out.append(await self.view(item.url))
        return tuple(out)

    # ------------------------------------------------------------------ helpers

    def _slug(self, repo: str | None) -> tuple[str, str]:
        """`"owner/name"`, or a bare `"name"` under the configured owner."""
        target = repo or self._repo
        if not target:
            raise GiteaError(
                "no Gitea repository to open a PR against: set `pr.forge_repo` in config, or pass "
                "`repo=`; guessing one would open a PR in the wrong project"
            )
        if "/" in target:
            owner, _, name = target.partition("/")
            return owner, name
        if not self._owner:
            raise GiteaError(
                f"repository {target!r} names no owner and `pr.forge_owner` is empty; Gitea's API "
                "path requires both"
            )
        return self._owner, target

    async def _label_ids(self, owner: str, name: str, labels: Sequence[str]) -> list[int]:
        """Gitea's create-PR payload takes label IDs, GitHub's `--label` takes names.

        Resolved rather than dropped: silently discarding a label the caller asked for is a lie the
        reviewer only discovers by not finding the PR in their filter. Only reached when `labels`
        is non-empty, so the default path costs nothing (Rule 2).
        """
        # Paginated: a repo with more than one page of labels would otherwise have its later
        # labels silently absent from `by_name`, misreporting a real label as "does not exist".
        by_name: dict[str, int] = {}
        page = 1
        while True:
            known = json.loads(
                await self._exec("GET", f"/repos/{owner}/{name}/labels?limit=100&page={page}")
            )
            if not isinstance(known, list):
                raise GiteaError(f"Gitea returned a non-list label index for {owner}/{name}")
            for row in known:
                if isinstance(row, dict) and "name" in row and "id" in row:
                    by_name[str(row["name"])] = int(row["id"])
            if len(known) < 100:
                break
            page += 1
        missing = [label for label in labels if label not in by_name]
        if missing:
            raise GiteaError(
                f"label(s) {missing} do not exist in {owner}/{name} (have {sorted(by_name)}); "
                "Gitea's API takes label IDs, and inventing one would label the wrong thing"
            )
        return [by_name[label] for label in labels]


def _read_body(body_file: Path) -> str:
    """Sync filesystem read, deliberately outside the async body (ruff ASYNC240)."""
    return body_file.read_text(encoding="utf-8")


def _split_status(stdout: str) -> tuple[int, str]:
    """Split curl's `-w '\\n%{http_code}'` trailer off the response body."""
    body, _, code = stdout.rstrip().rpartition("\n")
    try:
        return int(code.strip()), body.strip()
    except ValueError as exc:
        raise GiteaError(
            f"curl produced no HTTP status line: {redact_text(stdout)[:200]!r}"
        ) from exc


def _as_object(payload: str) -> Mapping[str, object]:
    """Decode a response that MUST be a JSON object, failing loudly when it is not."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise GiteaError(f"Gitea returned non-JSON: {redact_text(payload)[:200]!r}") from exc
    if not isinstance(data, dict):
        raise GiteaError(f"Gitea returned {type(data).__name__}, expected an object")
    return data
