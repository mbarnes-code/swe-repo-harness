"""Phase 4 steps 4–5: PR emission, and the PR STATE INGESTION nothing else performs (§3.4).

Step 5 is the reason this worker is not a thin create-a-PR wrapper. SPEC §3.4 step 5:
*"`MERGED` is a fact about GitHub, and until something reads it back nothing in this spec ever
writes it — which would deadlock the fleet at the first wave boundary."* Three gates consume
`PrState.MERGED` — the Phase 4 stacking precondition, the §3.5 `blocked_by` release, and the
§3.5.1 T1 stub trigger — and no worker can honestly produce it. So this worker **ingests** the
state before it acts on it: it reads every dependency PR in a non-terminal state back off the
forge, and the payload's believed state is used only for PRs the forge was not asked about. A
dependency the forge does not call `MERGED` **holds** this PR: nothing is created,
`status='partial'` records that the sync unit landed, and re-entry owes only the creation.
Assuming merge state is precisely the defect that deadlocked the fleet after wave 0.

**The forge is a payload field, not a client this worker constructs.** `pr.forge` names the
`vcs.forge.Forge` driver — `gh` for GitHub, `curl` against the API for Gitea — and `build_forge`
turns the name into one, so every step below depends on the Protocol and never on a vendor client
(CLAUDE.md guardrail 3). The credential travels as the PATH of a mode-600 `curl -K` file: a
payload is persisted, so a token carried on one would be a token in the state DB (§11.4).

**Draft is a verdict, not a style.** A PR is opened as a draft whenever the verification it
reports is anything other than a full-fidelity one:

* `equivalence == STUB_LIMITED` (§3.5.1) — the body leads with the stub banner, lists every
  `verified_against_stubs` coordinate with its fidelity tier, and names what the green build
  therefore does not prove;
* `equivalence == CLOSURE_SAMPLED` (§3.4) — the body names `rdeps_target_count`, the sample size
  and `rdeps_sample_seed`, so the reduction is auditable and reproducible;
* a `DEGRADED` repo, or any `stubs` row still `ACTIVE`/`SUPERSEDED` — `fleet pr --ready` refuses
  these anyway, and opening one as ready would put the refusal after the reviewer instead of
  before them.

**The verdict fields are rendered from data.** `VerificationReport` supplies every number in the
body; the model (`Role.PR_BODY`, `Role.PR_TITLE`) supplies prose only, and only when one is
injected. With no model the body is still complete — the banner, the counts and the dependency
list are code (Rule 5), so a run with the LLM disabled still opens an honest PR.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Final

from pydantic import Field

from fleet.bazel.query import sample_seed_for
from fleet.llm.calls import Evidence, write_pr_body, write_pr_title
from fleet.llm.client import LlmError
from fleet.models.base import FleetModel
from fleet.models.enums import (
    Equivalence,
    FailureClass,
    Phase,
    PrState,
    RepoStatus,
    StubFidelity,
    StubState,
)
from fleet.models.tasks import PullRequestDraft, TokenUsage, VerificationReport
from fleet.orchestrator.registry import register_worker
from fleet.util.proc import CommandRunner
from fleet.vcs import build_forge
from fleet.vcs.forge import Forge, ForgeError, PrStatus, PrSyncItem
from fleet.workers.base import (
    BaseWorker,
    WorkerBudget,
    WorkerContext,
    WorkerError,
    WorkerInput,
    WorkerOutput,
    WorkerResult,
    accumulate,
    loop_now,
)

__all__ = [
    "CREATE_UNIT",
    "READY_UNIT",
    "STUB_BANNER",
    "SYNC_UNIT",
    "DependencyPr",
    "PrwriterInput",
    "PrwriterOutput",
    "PrwriterWorker",
    "render_body",
]

SYNC_UNIT = "pr_sync"
CREATE_UNIT = "pr_create"
READY_UNIT = "pr_ready"

STUB_BANNER: Final = "Migrated against stubs — do not merge until these land"
"""§3.4 verbatim. Asserted by a test, so a reword that quietly drops the warning fails loudly."""

SAMPLED_BANNER: Final = "Verified against a SAMPLED reverse-dependency closure"
"""§3.4: the banner names `rdeps_target_count`, the sample size and `rdeps_sample_seed`."""

_HELD_STATES: Final[frozenset[StubState]] = frozenset({StubState.ACTIVE, StubState.SUPERSEDED})
"""`fleet pr --ready` refuses while any stub row is one of these (§12.37), so the PR opens as a
draft rather than being promoted and then refused."""


class DependencyPr(FleetModel):
    """One stacked dependency. `state` is what the harness BELIEVES; the forge decides."""

    repo_id: str = Field(min_length=1)
    url: str | None = Field(
        default=None, description="None ⇒ the dependency has no PR at all ⇒ unmerged by definition"
    )
    state: PrState = PrState.DRAFTED


class PrwriterInput(WorkerInput):
    """Everything one PR needs. Every verdict field comes from `report`, never from the caller."""

    report: VerificationReport
    wave_index: int = Field(default=0, ge=0)
    branch: str = Field(pattern=r"^migrate/[a-z0-9._-]+$")
    base: str = "integration"
    source_url: str = Field(min_length=1, description="Credential-free; redacted at §11.4")
    source_sha: str = Field(min_length=1)
    dependencies: list[DependencyPr] = Field(default_factory=list)
    repo_status: RepoStatus = Field(
        default=RepoStatus.SUCCEEDED,
        description="DEGRADED ⇒ draft-only, with the stub banner (§3.5)",
    )
    stub_states: dict[str, StubState] = Field(
        default_factory=dict, description="coord_key → state for every stub row not yet RESOLVED"
    )
    stub_fidelity: dict[str, StubFidelity] = Field(default_factory=dict)
    rdeps_sample_seed: str = Field(
        default="",
        description="`RdepverifyOutput.rdeps_sample_seed`. Carried on the payload because "
        "`VerificationReport` has no seed column (see `_seed_of`); empty re-derives it.",
    )
    contract_id: str | None = None
    scc_id: str | None = None
    member_repo_ids: list[str] = Field(default_factory=list)
    weak_edges: list[str] = Field(default_factory=list)
    relocation_summary: list[str] = Field(default_factory=list)
    human_intervention_notes: list[str] = Field(default_factory=list)
    collapsed_carriers: list[str] = Field(
        default_factory=list, description="Contract PR: carriers whose copy this hoist collapsed"
    )
    consumer_repo_ids: list[str] = Field(default_factory=list)
    dissolved_scc_id: str | None = None
    title: str | None = Field(default=None, description="Overrides the rendered/model title")
    labels: list[str] = Field(default_factory=list)
    gh_repo: str | None = None
    gh_bin: str = "gh"
    forge: str = "github"
    """Which `vcs.forge.Forge` driver opens this PR (`pr.forge`). A DRIVER, not a base URL: `gh`
    speaks the GitHub API and cannot talk to a self-hosted Gitea."""
    forge_url: str = ""
    """`pr.forge_url` — the instance root, e.g. `http://localhost:3001`. Unused by `github`."""
    forge_owner: str = ""
    """`pr.forge_owner` — Gitea's API path is `/repos/{owner}/{repo}/pulls`, with nothing to
    infer when the payload's `gh_repo` is a bare name."""
    forge_token_config: str = ""
    """PATH of the mode-600 `curl -K` file, never the token itself. This field carries a
    filename precisely because a payload is persisted and `attempts.command` persists argv: the
    credential is read by `curl` from disk after `execve`, so it reaches no payload, no argv and
    no error message (§11.4)."""
    ready: bool = Field(
        default=False,
        description="Promote out of draft when NOTHING forces a draft. Ignored otherwise: a "
        "disclosed reduction is cleared by a human, never by this worker.",
    )
    log_dir: str = "artifacts/logs"
    revalidation_round: int = Field(default=0, ge=0)


class PrwriterOutput(WorkerOutput):
    """The draft record, the observed dependency states, and — if held — what held it."""

    pr: PullRequestDraft | None = None
    held: bool = False
    unmerged_dependencies: list[str] = Field(default_factory=list)
    observed_states: dict[str, PrState] = Field(
        default_factory=dict,
        description="repo_id → the state the FORGE reported (or the believed one for a PR that "
        "was already terminal). This is the ingested fact §3.4 step 5 exists to produce.",
    )
    merge_commits: dict[str, str] = Field(default_factory=dict)
    draft: bool = True
    body_path: str = ""


@register_worker
class PrwriterWorker(BaseWorker[PrwriterInput, PrwriterOutput]):
    """Ingest dependency merge state, then open one stacked PR — draft unless nothing says else."""

    __slots__ = ("_runner",)

    name: ClassVar[str] = "prwriter"
    phase: ClassVar[Phase] = Phase.VERIFY
    input_model: ClassVar[type[WorkerInput]] = PrwriterInput
    output_model: ClassVar[type[WorkerOutput]] = PrwriterOutput
    budget: ClassVar[WorkerBudget] = WorkerBudget(wall_clock_s=600, max_subprocesses=4)

    def __init__(self, *, runner: CommandRunner | None = None) -> None:
        """`runner` is the forge's subprocess seam — `gh` for GitHub, `curl` for Gitea, chosen
        per payload by `build_forge`. The prose model is NOT a constructor argument: it is
        `ctx.llm` (§7.1). Prose is optional the way a *call* is optional — an `LlmError` falls back
        to the rendered body — never the way a mis-wired constructor is."""
        self._runner = runner

    # ------------------------------------------------------------------ preconditions

    async def preconditions_hold(self, ctx: WorkerContext, payload: PrwriterInput) -> bool:
        """Never open a PR for a verification that did not pass, and never re-open a closed one.

        The `verdict` check is not paranoia: §3.4's success criterion is Phase 3 green *and*
        `bazel test` green over the verified target set *and* a resolvable PR url, so a `FAIL`
        report reaching this worker means something upstream mis-sequenced — and a PR opened on it
        would present a red verification as reviewable work.

        The report must also describe THIS repo: a payload assembled from another repo's report
        would render another repo's numbers into this body, which is the one error a reviewer
        cannot catch by reading the PR.
        """
        if payload.report.verdict != "PASS":
            return False
        if payload.report.repo_id != ctx.repo_id:
            return False
        return payload.repo_status not in (
            RepoStatus.REQUIRES_HUMAN_INTERVENTION,
            RepoStatus.SKIPPED,
        )

    # ------------------------------------------------------------------ the work

    async def run(
        self, ctx: WorkerContext, payload: PrwriterInput
    ) -> WorkerResult[PrwriterOutput]:
        # The one place this worker chooses a code host, and it chooses by NAME from the
        # payload (`pr.forge`) rather than by constructing a driver: guardrail 3's dependency
        # inversion is only real if the high-level step depends on `Forge` and never on a vendor
        # client. `curl_config` is a PATH — see `PrwriterInput.forge_token_config`.
        gh: Forge = build_forge(
            payload.forge,
            runner=self._runner,
            base_url=payload.forge_url,
            owner=payload.forge_owner,
            repo=payload.gh_repo,
            curl_config=Path(payload.forge_token_config) if payload.forge_token_config else None,
            gh_bin=payload.gh_bin,
            cwd=Path(ctx.workdir),
            deadline=ctx.deadline,
        )
        output = PrwriterOutput()
        usage = TokenUsage()
        draft = self._must_be_draft(payload)
        output.draft = draft
        units = [SYNC_UNIT, CREATE_UNIT] + ([READY_UNIT] if payload.ready and not draft else [])

        # -- step 5 FIRST: merge state is ingested, never assumed ------------------------
        try:
            observed = await self._sync(gh, payload)
        except ForgeError as exc:  # `ForgeUnavailableError` too: both re-queue, neither judges
            return self._gh_failure(output, exc, FailureClass.TRANSIENT_INFRA, units)
        output.observed_states = {dep: status.state for dep, status in observed.items()}
        output.merge_commits = {
            dep: status.merge_commit_sha
            for dep, status in observed.items()
            if status.merge_commit_sha is not None
        }
        unmerged = sorted(
            dep for dep, status in observed.items() if status.state is not PrState.MERGED
        )

        if unmerged:
            # §3.4 precondition, enforced against what the forge said rather than against hope.
            # HELD, not failed: the dependency may merge in five minutes, and burning an attempt
            # on someone else's review latency is what `pr.merge_wait_timeout_s` exists to bound.
            output.held = True
            output.unmerged_dependencies = unmerged
            return WorkerResult[PrwriterOutput](
                status="partial",
                output=output,
                completed_units=[SYNC_UNIT],
                remaining_units=[u for u in units if u != SYNC_UNIT],
                evidence=[f"unmerged:{dep}" for dep in unmerged],
            )

        if ctx.cancelled() or ctx.expired(loop_now()):
            output.held = True
            return WorkerResult[PrwriterOutput](
                status="partial",
                output=output,
                completed_units=[SYNC_UNIT],
                remaining_units=[u for u in units if u != SYNC_UNIT],
            )

        # -- step 4: body from data, prose from the model, PR through a file -------------
        title, body, usage = await self._compose(ctx, payload, draft=draft)
        body_path = self._body_path(ctx, payload)
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_text(body, encoding="utf-8")
        output.body_path = str(body_path)

        try:
            url = await gh.create_pr(
                base=payload.base,
                head=payload.branch,
                title=title,
                body_file=body_path,
                draft=draft,
                repo=payload.gh_repo,
                labels=payload.labels,
            )
        except ForgeError as exc:
            return self._gh_failure(
                output, exc, FailureClass.TRANSIENT_INFRA, [CREATE_UNIT], completed=[SYNC_UNIT],
                usage=usage,
            )

        completed = [SYNC_UNIT, CREATE_UNIT]
        state = PrState.DRAFTED if draft else PrState.OPEN
        if READY_UNIT in units:
            try:
                await gh.mark_ready(url)
            except ForgeError as exc:
                return self._gh_failure(
                    output, exc, FailureClass.TRANSIENT_INFRA, [READY_UNIT], completed=completed,
                    usage=usage,
                )
            completed.append(READY_UNIT)
            state = PrState.OPEN

        output.pr = PullRequestDraft(
            run_id=ctx.run_id,
            repo_id=ctx.repo_id,
            contract_id=payload.contract_id,
            scc_id=payload.scc_id,
            member_repo_ids=list(payload.member_repo_ids),
            wave_index=payload.wave_index,
            branch=payload.branch,
            base=payload.base,
            title=title,
            body=body,
            depends_on_repos=[dep.repo_id for dep in payload.dependencies],
            depends_on_prs=[dep.url for dep in payload.dependencies if dep.url is not None],
            stubbed_deps=list(payload.report.verified_against_stubs),
            equivalence=payload.report.equivalence,
            unresolved_stub_states=dict(payload.stub_states),
            revalidation_round=payload.revalidation_round,
            weak_edges=list(payload.weak_edges),
            source_url=payload.source_url,
            source_sha=payload.source_sha,
            state=state,
            url=url,
        )
        return WorkerResult[PrwriterOutput](
            status="ok",
            output=output,
            completed_units=completed,
            usage=usage,
            evidence=[url, str(body_path)],
        )

    # ------------------------------------------------------------------ step 5

    async def _sync(self, gh: Forge, payload: PrwriterInput) -> dict[str, PrStatus]:
        """Read every non-terminal dependency PR back; believe the forge, not the payload.

        A dependency with no url is reported as its believed state and can therefore never be
        `MERGED` — a PR that was never opened cannot have been merged, and defaulting it to OPEN
        (or, worse, skipping it) is how an unopened dependency stops holding its dependents.
        """
        pollable = [dep for dep in payload.dependencies if dep.url is not None]
        statuses = {
            status.url: status
            for status in await gh.sync(
                PrSyncItem(url=dep.url or "", state=dep.state) for dep in pollable
            )
        }
        observed: dict[str, PrStatus] = {}
        for dep in payload.dependencies:
            if dep.url is None:
                observed[dep.repo_id] = PrStatus(url="", state=dep.state)
                continue
            observed[dep.repo_id] = statuses.get(dep.url, PrStatus(url=dep.url, state=dep.state))
        return observed

    # ------------------------------------------------------------------ step 4

    def _must_be_draft(self, payload: PrwriterInput) -> bool:
        """Every disclosed reduction forces a draft; a human clears it, never this worker."""
        return (
            payload.report.equivalence is not Equivalence.FULL
            or payload.repo_status is RepoStatus.DEGRADED
            or any(state in _HELD_STATES for state in payload.stub_states.values())
            or bool(payload.report.verified_against_stubs)
        )

    async def _compose(
        self, ctx: WorkerContext, payload: PrwriterInput, *, draft: bool
    ) -> tuple[str, str, TokenUsage]:
        """Title and body. Code renders every verdict; the model may only add prose."""
        usage = TokenUsage()
        notes = ""
        title = payload.title or _default_title(payload, repo_id=ctx.repo_id)
        evidence: Evidence = {
            "repo_id": ctx.repo_id,
            "dest": payload.report.repo_id,
            "wave_index": payload.wave_index,
            "equivalence": payload.report.equivalence.value,
            "verdict": payload.report.verdict,
            "depends_on": [dep.repo_id for dep in payload.dependencies],
            "relocation_summary": list(payload.relocation_summary),
        }
        try:
            prose = await write_pr_body(ctx.llm, evidence, budget=ctx.budget)
        except LlmError:
            prose = None
        if prose is not None:
            notes = prose.value.body
            usage = accumulate(usage, prose.usage)
        if payload.title is None:
            try:
                proposed = await write_pr_title(ctx.llm, evidence, budget=ctx.budget)
            except LlmError:
                proposed = None
            if proposed is not None:
                title = proposed.value.title
                usage = accumulate(usage, proposed.usage)
        body = render_body(payload, repo_id=ctx.repo_id, draft=draft, notes=notes)
        return title[:120], body, usage

    def _body_path(self, ctx: WorkerContext, payload: PrwriterInput) -> Path:
        return (
            Path(payload.log_dir)
            / str(ctx.run_id)
            / f"{ctx.repo_id}-{payload.revalidation_round}-pr-body.md"
        )

    def _gh_failure(
        self,
        output: PrwriterOutput,
        exc: ForgeError,
        failure_class: FailureClass,
        remaining: list[str],
        *,
        completed: list[str] | None = None,
        usage: TokenUsage | None = None,
    ) -> WorkerResult[PrwriterOutput]:
        """The forge call failed. Loud and typed — a PR poll that silently reports "unchanged"
        on a malformed reply is how a merged dependency stays blocked forever (§3.4 step 5).

        `stderr_tail` is the driver's own message, so it already names the forge that failed
        rather than assuming `gh`; nothing here re-labels it."""
        return WorkerResult[PrwriterOutput](
            status="failed",
            output=output,
            completed_units=completed or [],
            remaining_units=[u for u in remaining if u not in (completed or [])],
            usage=usage or TokenUsage(),
            error=WorkerError(
                failure_class=failure_class,
                retryable=True,
                stderr_tail=str(exc),
                exception_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
            ),
        )


def _default_title(payload: PrwriterInput, *, repo_id: str) -> str:
    """A deterministic title, so a run with no model still opens a PR a human can read."""
    what = payload.contract_id or payload.scc_id or repo_id
    return f"[fleet wave {payload.wave_index}] migrate {what} into the monorepo"


def render_body(
    payload: PrwriterInput, *, repo_id: str, draft: bool, notes: str = ""
) -> str:
    """The PR body, rendered from `VerificationReport` and the payload — never from the model.

    The banners come first because they are the part a reviewer must not scroll past: §3.4 says a
    `STUB_LIMITED` body *leads* with the stub banner and names what the green build does not
    prove, and a `CLOSURE_SAMPLED` body names the count, the sample and the seed so the reduction
    is reproducible. A body that buried either under a model's prose would be a disclosure in name.
    """
    report = payload.report
    lines: list[str] = []

    if report.equivalence is Equivalence.STUB_LIMITED:
        lines += [f"> **{STUB_BANNER}**", ""]
        for coord in sorted(report.verified_against_stubs):
            tier = report.stub_fidelity.get(coord)
            lines.append(f"> - `{coord}` — fidelity `{tier.value if tier else 'UNKNOWN'}`")
        lines += [
            ">",
            "> This build is green **against stubs**, which proves it compiles against the stub's",
            "> surface — not that it works against the real dependency.",
            "",
        ]
    if report.rdeps_truncated:
        lines += [
            f"> **{SAMPLED_BANNER}**",
            ">",
            f"> `rdeps_target_count` = {report.rdeps_target_count}; tested "
            f"{report.rdeps_tested}; `rdeps_sample_seed` = `{_seed_of(payload)}`.",
            "> The untested remainder is a disclosed reduction, not a pass.",
            "",
        ]

    lines += [
        f"## Migration of `{repo_id}` (wave {payload.wave_index})",
        "",
        f"- Source: {payload.source_url} @ `{payload.source_sha}`",
        f"- Base: `{payload.base}` ← `{payload.branch}`",
        f"- Equivalence: `{report.equivalence.value}`",
        f"- Draft: `{draft}`",
    ]
    if payload.contract_id is not None:
        lines += [
            f"- Hoisted contract: `{payload.contract_id}` "
            f"(`Hoisted-Contract:` trailer on the merge commit)",
            f"- Owning repo: `{repo_id}`",
        ]
        if payload.collapsed_carriers:
            lines.append(f"- Carriers collapsed: {_csv(payload.collapsed_carriers)}")
        if payload.consumer_repo_ids:
            lines.append(
                f"- Consumers whose checked-in generated code this replaces with a `//` label: "
                f"{_csv(payload.consumer_repo_ids)}"
            )
        if payload.dissolved_scc_id is not None:
            lines.append(f"- Dissolves cycle: `{payload.dissolved_scc_id}`")
    if payload.scc_id is not None:
        lines.append(f"- ATOMIC_WAVE SCC `{payload.scc_id}`: {_csv(payload.member_repo_ids)}")

    lines += [
        "",
        "### Verification",
        "",
        f"- `bazel build`: {'PASS' if report.build_ok else 'FAIL'}",
        f"- `bazel test` (own): {'PASS' if report.test_ok else 'FAIL'}",
        f"- rdeps closure: {report.rdeps_target_count} target(s), {report.rdeps_tested} tested, "
        f"truncated=`{report.rdeps_truncated}`",
        f"- rdeps query: `{report.rdeps_query or 'n/a'}`",
        f"- Verdict: **{report.verdict}**",
    ]
    if payload.revalidation_round:
        lines.append(f"- Revalidation round: {payload.revalidation_round}")

    lines += ["", "### Dependencies (stacked, topological)", ""]
    if payload.dependencies:
        lines += [
            f"- `{dep.repo_id}` — {dep.url or 'no PR yet'} (`{dep.state.value}`)"
            for dep in payload.dependencies
        ]
    else:
        lines.append("- none — this node is a graph source")

    if payload.relocation_summary:
        lines += ["", "### Relocation map", "", *[f"- {row}" for row in payload.relocation_summary]]
    if payload.weak_edges:
        lines += [
            "",
            "### Weak edges (below `min_confidence`)",
            "",
            *[f"- `{edge}`" for edge in payload.weak_edges],
        ]
    if payload.human_intervention_notes:
        lines += [
            "",
            "### REQUIRES_HUMAN_INTERVENTION notes for dependents",
            "",
            *[f"- {note}" for note in payload.human_intervention_notes],
        ]
    if notes:
        lines += ["", "### Migration notes", "", notes]
    return "\n".join(lines) + "\n"


def _seed_of(payload: PrwriterInput) -> str:
    """The recorded sample seed, or the one `bazel/query.py` would re-derive from the query.

    SPEC_GAP: §3.4 names `rdeps_sample_seed` as a `VerificationReport` field and `models/tasks.py`
    has no such column, so the seed reaches the banner on the payload. The fallback is the same
    `sample_seed_for(query)` the sampler uses when no seed is pinned, which is what makes the
    banner name the seed a resume would reproduce rather than a fresh random one.
    """
    return payload.rdeps_sample_seed or sample_seed_for(payload.report.rdeps_query)


def _csv(values: list[str]) -> str:
    return ", ".join(f"`{value}`" for value in sorted(values)) or "none"
