"""`gh` CLI wrapper: PR creation and — the part nothing else supplies — PR STATE INGESTION.

SPEC §3.4 step 5 is blunt about why the second half exists: *"`MERGED` is a fact about GitHub, and
until something reads it back nothing in this spec ever writes it — which would deadlock the fleet
at the first wave boundary."* Three gates consume `PrState.MERGED` (the Phase 4 stacking
precondition, the §3.5 `blocked_by` release, and the §3.5.1 T1 stub trigger) and no worker can
honestly produce it, so `sync()` is a step, not an assumption: for every PR in a non-terminal state
it runs `gh pr view <url> --json state,mergedAt,mergeCommit` and returns the parsed result.

**This module reads; it never writes state.** Results are returned to the caller, which persists
them through the single writer (§11.5). A `pr_merged` event — emitted by that writer, not guessed
by a worker — is what unblocks a dependent.

**Testability without `gh` and without network.** Every call goes through an injected
`CommandRunner`, and the parsing is a pure function (`parse_pr_view`) over the JSON text, so the
state machine that gates the whole fleet is verifiable against recorded `gh` output on a host with
no `gh` and no credentials. `available()` is the honest gate for the tests that genuinely need the
binary — they skip loudly rather than passing vacuously.

**Bodies go through a file, never through argv.** `--body-file` keeps a PR body — which quotes
build logs and repo URLs — out of the process table, and out of any error message built from argv.

**One driver among two.** `GitHubCli` implements `vcs/forge.Forge`; `vcs/gitea.GiteaForge` is the
other. The PR types (`PrStatus`, `PrSyncItem`, `NON_TERMINAL_STATES`) moved to `forge.py` because
they describe a PR rather than GitHub, and are re-exported here so every existing import — and
every existing test — keeps working unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Final

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
    "NON_TERMINAL_STATES",
    "PR_VIEW_FIELDS",
    "GhError",
    "GhUnavailableError",
    "GitHubCli",
    "PrStatus",
    "PrSyncItem",
    "parse_pr_view",
]

PR_VIEW_FIELDS: Final = "state,mergedAt,mergeCommit"
"""Exactly the §3.4 step 5 field set. Asking for more would make the poll — one call per open PR
per `pr.poll_interval_s` across a 250-PR fleet — pay for data no gate reads."""

_GH_STATE_MAP: Final[Mapping[str, PrState]] = {
    "OPEN": PrState.OPEN,
    "MERGED": PrState.MERGED,
    "CLOSED": PrState.CLOSED,
}


class GhError(ForgeError):
    """A `gh` invocation failed or returned something unparseable. Loud (Rule 11): a PR poll that
    silently returns "unchanged" on a malformed reply is how a merged dependency stays blocked."""


class GhUnavailableError(GhError, ForgeUnavailableError):
    """`gh` is missing or unauthenticated. Named separately because the operator response differs
    from a failed call, and because tests skip on exactly this condition."""


def _parse_timestamp(raw: object) -> datetime | None:
    """gh emits RFC3339 with a `Z`, and an unmerged PR emits `null` or `""`."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GhError(f"unparseable mergedAt from gh: {raw!r}") from exc


def parse_pr_view(url: str, payload: str) -> PrStatus:
    """Parse `gh pr view --json state,mergedAt,mergeCommit` output into a `PrStatus`.

    Pure and public: this is the function that decides whether a wave advances, so it is tested
    against recorded `gh` JSON rather than against a live forge. An unknown `state` raises instead
    of defaulting to OPEN — a silently-wrong OPEN would hold every dependent's Phase 4 forever.
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise GhError(f"gh pr view returned non-JSON for {url}: {payload[:200]!r}") from exc
    if not isinstance(data, dict):
        raise GhError(f"gh pr view returned {type(data).__name__}, expected an object, for {url}")

    raw_state = data.get("state")
    if not isinstance(raw_state, str) or raw_state.upper() not in _GH_STATE_MAP:
        raise GhError(
            f"gh reported an unknown PR state {raw_state!r} for {url}; refusing to guess, because "
            "an assumed OPEN blocks every dependent's Phase 4 precondition (§3.4 step 5)"
        )
    merge_commit = data.get("mergeCommit")
    sha: str | None = None
    if isinstance(merge_commit, dict):
        oid = merge_commit.get("oid")
        sha = oid if isinstance(oid, str) and oid else None
    return PrStatus(
        url=url,
        state=_GH_STATE_MAP[raw_state.upper()],
        merged_at=_parse_timestamp(data.get("mergedAt")),
        merge_commit_sha=sha,
    )


class GitHubCli:
    """The `gh` surface the harness uses, with the runner injected (CLAUDE.md guardrail 3)."""

    def __init__(
        self,
        *,
        runner: CommandRunner = run,
        gh_bin: str = "gh",
        cwd: Path | None = None,
        deadline: float | None = None,
        timeout_s: float | None = 120.0,
    ) -> None:
        self._runner = runner
        self._gh = gh_bin
        self.cwd = cwd
        self.deadline = deadline
        self.timeout_s = timeout_s

    def argv(self, args: Sequence[str]) -> tuple[str, ...]:
        return (self._gh, *args)

    async def _exec(self, args: Sequence[str], *, check: bool = True) -> str:
        parts = self.argv(args)
        result = await self._runner(
            parts, cwd=self.cwd, deadline=self.deadline, timeout_s=self.timeout_s
        )
        if not result.started or result.exit_code == 127:
            raise GhUnavailableError(
                f"{self._gh} is not available (exit {result.exit_code}); PR emission and PR state "
                "ingestion both require it (§3.4)"
            )
        if check and not result.ok:
            raise GhError(
                f"gh {' '.join(redact_argv(parts)[1:])} failed (exit {result.exit_code}): "
                f"{result.stderr_tail}"
            )
        return result.stdout_tail.strip()

    async def available(self) -> bool:
        """Is `gh` installed AND authenticated? Both, because an unauthenticated `gh` fails at the
        first API call with an error a poll loop would otherwise retry forever."""
        try:
            await self._exec(["auth", "status"])
        except GhError:
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
        """`gh pr create` → the PR URL.

        `draft=True` is not cosmetic: a `STUB_LIMITED` or `CLOSURE_SAMPLED` verification MUST open
        a draft (§3.4), because a disclosed reduction is something a human clears, never a silent
        pass.
        """
        args = [
            "pr",
            "create",
            "--base",
            base,
            "--head",
            head,
            "--title",
            title,
            "--body-file",
            str(body_file),
        ]
        if repo is not None:
            args += ["--repo", repo]
        if draft:
            args.append("--draft")
        for label in labels:
            args += ["--label", label]
        out = await self._exec(args)
        return _extract_url(out)

    async def edit_body(self, url: str, body_file: Path, *, title: str | None = None) -> None:
        """Regenerate an existing PR's body (§3.5.1 resolution re-uses the same PR, so its number,
        URL, and review history survive a revalidation round)."""
        args = ["pr", "edit", url, "--body-file", str(body_file)]
        if title is not None:
            args += ["--title", title]
        await self._exec(args)

    async def mark_ready(self, url: str) -> None:
        """`gh pr ready` — promotion out of draft. The refusal-while-stubs-are-active rule lives in
        the CLI layer (§3.5.1); this is only the forge call it makes once that check passes."""
        await self._exec(["pr", "ready", url])

    async def view(self, url: str) -> PrStatus:
        """`gh pr view <url> --json state,mergedAt,mergeCommit` → `PrStatus`."""
        return parse_pr_view(url, await self._exec(["pr", "view", url, "--json", PR_VIEW_FIELDS]))

    async def sync(self, prs: Iterable[PrSyncItem]) -> tuple[PrStatus, ...]:
        """Poll every NON-TERMINAL PR and return what the forge says (`fleet pr --sync`).

        Sequential on purpose: the poll is bounded by the `git_net` semaphore in §3.4, and a
        250-PR fleet issuing 250 concurrent `gh` calls would trip GitHub's secondary rate limit and
        turn a status read into an outage. Results are returned for the single writer to persist;
        this function writes nothing.
        """
        out: list[PrStatus] = []
        for item in prs:
            if item.state in NON_TERMINAL_STATES:
                out.append(await self.view(item.url))
        return tuple(out)


def _extract_url(stdout: str) -> str:
    """`gh pr create` prints the URL as its last output line; anything else is a failure to report
    loudly rather than a `None` url that Phase 4's success criterion would later reject."""
    for line in reversed([line.strip() for line in stdout.splitlines() if line.strip()]):
        if line.startswith(("http://", "https://")):
            return line
    raise GhError(f"gh pr create printed no PR URL: {redact_text(stdout)[:200]!r}")
