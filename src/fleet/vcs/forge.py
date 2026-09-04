"""The forge seam: one `Protocol` the PR path depends on, and the types both drivers share.

SPEC §3.4 is written against "the forge", not against GitHub, but the harness had exactly one
driver — `vcs/github.py`, shelling `gh` — wired straight into `workers/prwriter.py` and
`cli._pr_impl`. `gh` speaks the GitHub API and nothing else, so an operator running a self-hosted
Gitea had no PR path at all. This module is the dependency inversion CLAUDE.md guardrail 3 asks
for: high-level orchestration depends on `Forge`, and `GitHubCli` / `GiteaForge` are two
implementations of it.

**The Protocol is exactly what the harness calls, and nothing more** (Rule 2). Read off the two
call sites rather than guessed:

* `create_pr` — `prwriter.run` step 4.
* `view` / `sync` — `prwriter._sync` and `cli._pr_sync_impl`. This pair is load-bearing:
  §3.4 step 5 is blunt that *"`MERGED` is a fact about the forge, and until something reads it back
  nothing in this spec ever writes it — which would deadlock the fleet at the first wave
  boundary"*, and three gates consume `PrState.MERGED`.
* `mark_ready` — `prwriter`'s `READY_UNIT` and `fleet pr --ready`.
* `edit_body` — `cli._promote_one_pr` (§12.38/D94): once a stub blocking an already-open draft PR
  resolves, the resolution re-uses the SAME PR (its number, url and review history survive), so
  the body must be regenerated in place rather than a new PR opened. `github.py` has carried this
  method since before it had a caller; `gitea.py` gained it with this Protocol addition.
* `available` — the honest probe the CLI and the tests gate on, so a missing binary or an
  unreachable forge fails loudly instead of passing vacuously.

`PrStatus`, `PrSyncItem` and `NON_TERMINAL_STATES` live here rather than in `github.py` because
they describe *a PR*, not *GitHub*; `github.py` re-exports them so every existing import keeps
working.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from fleet.models.enums import PrState
from fleet.vcs.git import GitError

__all__ = [
    "FORGE_NAMES",
    "NON_TERMINAL_STATES",
    "Forge",
    "ForgeError",
    "ForgeUnavailableError",
    "PrStatus",
    "PrSyncItem",
]

FORGE_NAMES: Final[tuple[str, ...]] = ("github", "gitea")
"""The drivers that exist. `settings.PrSection` validates `pr.forge` against this tuple, so an
unknown name is a STARTUP error naming the value — the same discipline §7.7 backends and §7.4
rewrite engines already get, and for the same reason: discovering it in wave 7 is discovering it
after 250 repos have been transformed."""

NON_TERMINAL_STATES: Final[frozenset[PrState]] = frozenset(
    {PrState.DRAFTED, PrState.OPEN, PrState.HELD}
)
"""What `sync()` polls. `MERGED` and `CLOSED` are terminal on the forge: re-polling them spends the
`git_net` semaphore on an answer that cannot change."""


class ForgeError(GitError):
    """A forge call failed or returned something unparseable. Loud (Rule 11): a PR poll that
    silently returns "unchanged" on a malformed reply is how a merged dependency stays blocked.

    Rooted at `GitError` so the existing `FailureClass` classification in `workers/prwriter.py`
    keeps working unchanged for both drivers.
    """


class ForgeUnavailableError(ForgeError):
    """The forge cannot be reached or the client is unauthenticated. Named separately because the
    operator response differs from a failed call, and because tests skip on exactly this."""


@dataclass(frozen=True, slots=True)
class PrStatus:
    """One PR as the forge currently reports it — the only place `MERGED` can come from.

    `merge_commit_sha` is kept because a merged PR's commit is the pointer Phase 4 records; a
    `MERGED` state with no commit is a partial answer and callers can detect it here.
    """

    url: str
    state: PrState
    merged_at: datetime | None = None
    merge_commit_sha: str | None = None

    @property
    def is_merged(self) -> bool:
        return self.state is PrState.MERGED

    @property
    def is_terminal(self) -> bool:
        return self.state not in NON_TERMINAL_STATES


@dataclass(frozen=True, slots=True)
class PrSyncItem:
    """One row of the poll set: the PR's url and the state the harness currently believes."""

    url: str
    state: PrState = PrState.DRAFTED


@runtime_checkable
class Forge(Protocol):
    """What `PrwriterWorker` and `fleet pr` need from a code-hosting service.

    `runtime_checkable` so a test can assert structurally that a driver still satisfies the seam.
    That check is shallow by design (it verifies the members exist, not their signatures), so the
    accompanying test compares `inspect.signature` too — a driver that renamed a keyword argument
    would otherwise pass `isinstance` and fail at the one call site that matters.
    """

    async def available(self) -> bool:
        """Is the forge reachable AND are we authenticated to it? Both, because an unauthenticated
        client fails at the first API call with an error a poll loop would retry forever."""
        ...

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
        """Open one PR and return its URL.

        `body_file` rather than a `body` string is part of the contract, not a convenience: a PR
        body quotes build logs and repo URLs, and §11.4 keeps that out of the process table and out
        of any error message built from argv.

        `draft=True` is a verdict, not a style: a `STUB_LIMITED` or `CLOSURE_SAMPLED` verification
        MUST open a draft (§3.4), because a disclosed reduction is something a human clears.
        """
        ...

    async def mark_ready(self, url: str) -> None:
        """Promote out of draft. The refusal-while-stubs-are-active rule lives in the CLI layer
        (§3.5.1); this is only the forge call it makes once that check passes."""
        ...

    async def edit_body(self, url: str, body_file: Path, *, title: str | None = None) -> None:
        """Regenerate an existing PR's body (§3.5.1 resolution re-uses the same PR, so its
        number, url, and review history survive a revalidation round)."""
        ...

    async def view(self, url: str) -> PrStatus:
        """Read one PR's state back from the forge. The sole source of `PrState.MERGED`."""
        ...

    async def sync(self, prs: Iterable[PrSyncItem]) -> tuple[PrStatus, ...]:
        """Poll every NON-TERMINAL PR in `prs` and return what the forge says.

        Sequential on purpose: a 250-PR fleet issuing 250 concurrent calls turns a status read into
        an outage (GitHub's secondary rate limit; a single-box Gitea's request pool). Results are
        returned for the single writer (§11.5) to persist; an implementation writes no state.
        """
        ...
