"""Work, results, verification (SPEC §5.4)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from fleet.models.base import FleetModel, TruncatedStr, utcnow
from fleet.models.enums import (
    ContextPolicy,
    Equivalence,
    FailureClass,
    ModelTier,
    Phase,
    PrState,
    StructuredOutputMode,
    StubFidelity,
    StubState,
    TaskKind,
    TransformTier,
)
from fleet.models.graph import EdgeKey, SccId
from fleet.models.repo import RepoId

MAX_ATTEMPTS = 3  # ADR-0014's DEFAULT ladder length, and nothing more. It is a policy value, so
# it is never compiled into a field constraint: `le=MAX_ATTEMPTS` on an `attempt` field would
# make the spec's own promise that the ladder is per-run configurable (§9) false for any ladder
# longer than 3, and would make a LOWERED constant retroactively unable to load historical rows.
# The ceiling is enforced at runtime against the owning `TransformTask.max_attempts`.

# ADR-0021: the anti-anchoring default ladder. Rung index == attempt number.
# Overridable per run via `transform.ladder` (§9) and `fleet transform --context-policy` (§10).
DEFAULT_LADDER: tuple[ContextPolicy | None, ...] = (
    None,                                            # attempt 1: deterministic, no prompt
    ContextPolicy.EVIDENCE_ONLY,                     # attempt 2: WORKHORSE tier, fresh slate
    ContextPolicy.EVIDENCE_PLUS_REJECTED_APPROACHES,  # attempt 3: HEAVY tier, pruned search space
)


class TokenUsage(FleetModel):
    role: str = ""
    tier: ModelTier | None = None      # ADR-0023: which tier the role resolved to
    backend: str = ""                  # ADR-0023: registered backend name that actually answered
    model_id: str = ""                 # MUST echo `target.model_id` verbatim — see below
    # `model_id` is the CONFIGURED id from config/models.yaml, NOT the id the transport resolved
    # or served the call as. A backend adapter that sets it from the server's reported name (an
    # API response's `model` field, say) breaks the LLM cache outright: the READ key is built from
    # the config string (`cache._key_parts`) and the WRITE key from `usage.model_id`
    # (`cache._store_response`), so the two disagree on EVERY call — a permanent, silent 100% miss
    # that is indistinguishable from a cold cache, because `attempts.llm_cache_hit` simply stays 0.
    # (`_target_for` likewise matches on (backend, model_id) and stops finding the answering
    # target, so `effort` falls back to the primary's.) Reporting the served id is a legitimate
    # want — it just needs a SEPARATE field or a log line, never this one.
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)
    # A locally-served model has no price. `cost_usd = 0.0` with a non-empty `backend` is a
    # legitimate free call, NOT a cache hit; `attempts.llm_cache_hit` is the only cache signal
    # (§11.2, §11.6), so a local profile does not silently look like a fully-cached run.
    llm_cache_lookups: int = Field(default=0, ge=0)
    llm_cache_hits: int = Field(default=0, ge=0)
    # The `llm_cache` (§11.6) signal, carried on the data that already flows from the call to the
    # `attempts` row. COUNTERS and not a `bool`, for a measured reason: `workers/base.py`'s
    # `accumulate` is used as a running fold seeded with a zero `TokenUsage()`
    # (`base.py`, `buildgen.py`, `prwriter.py`, `rewrite.py` all open with `usage = TokenUsage()`),
    # so a boolean AND over that fold is `False` for every attempt that ever existed and a
    # boolean OR is any-hit. Two summed counters have no identity element to poison, and they
    # keep `accumulate` doing exactly what its own docstring says it does: summing.
    # NOT `cache_read_tokens`' neighbourhood: that field is the PROVIDER's prompt cache. These
    # two count lookups against `llm_cache`, the harness's own store.
    llm_failovers: int = Field(default=0, ge=0)
    # ADR-0107, §11.8, §12.43(i). Backend hops spent inside the call that produced THIS usage —
    # `index` in `LadderModelClient.complete()`'s target loop at the point the call succeeded.
    # Summed by `accumulate`, exactly like the two counters above: `schema.sql`'s comment
    # ("backend hops spent inside THIS attempt") is a total across the whole attempt, not a
    # per-call flag, and a rung making several LLM calls can fail over independently on each.

    @property
    def all_served_from_llm_cache(self) -> bool:
        """The value `attempts.llm_cache_hit` takes for this (possibly accumulated) usage.

        ALL-hit, not any-hit, and that is forced rather than chosen: `state/schema.sql`'s column
        comment states `1 => cost_usd = 0`, and `accumulate` SUMS `cost_usd`. One miss in a
        multi-call attempt therefore leaves `cost_usd > 0`, so an any-hit flag would publish a
        row that contradicts the schema's own invariant.

        `llm_cache_lookups > 0` is the other half and is not a formality: a DETERMINISTIC rung, a
        `--llm-cache off` run and a default-constructed `TokenUsage` all reach here with zero
        lookups, and `all()` over nothing is `True`. Without this guard every attempt that never
        consulted the cache would report itself fully cached — the `cost_usd == 0` conflation the
        comment above exists to forbid, arriving through a different door.
        """
        return self.llm_cache_lookups > 0 and self.llm_cache_hits == self.llm_cache_lookups


class ModelCapabilities(FleetModel):
    """What a backend TARGET can actually do (ADR-0023). Declared in code per backend, merged
    with `capabilities_override` from the target's `config/models.yaml` entry, and probed on
    demand by `fleet models check` — never auto-probed at run start, because a run's plan may
    not depend on a network call."""

    supports_tools: bool = False
    supports_json_schema: bool = False
    supports_system_prompt: bool = True
    supports_streaming: bool = False
    supports_constrained_decoding: bool = False   # server-side grammar / guided JSON
    max_context: int = Field(default=8192, gt=0)
    max_output_tokens: int = Field(default=4096, gt=0)
    structured_output_modes: tuple[StructuredOutputMode, ...] = (StructuredOutputMode.PROMPTED,)
    # Ordered best-first; the negotiator (§7.7) takes the first entry it can honour. PROMPTED is
    # always present — it is the floor, not an opt-in.


class Price(FleetModel):
    """A target's declared token price, USD per million tokens (§9 rule 5). `price(target, n)`
    in §11.2 is exactly `(in_per_mtok * n_in + out_per_mtok * n_out) / 1e6`."""

    in_per_mtok: float = Field(ge=0.0)
    out_per_mtok: float = Field(ge=0.0)


class BackendTarget(FleetModel):
    """One entry in a tier's ordered backend list (`config/models.yaml`, §9)."""

    backend: str = Field(min_length=1, description="Must exist in the §7.7 backend registry")
    model_id: str = Field(min_length=1, description="Opaque to the harness; config data only")
    base_url: str | None = None          # required by `openai_compatible`; ignored by others
    api_key_env: str | None = None       # NAME of the env var; never the value (§11.4)
    region: str | None = None            # bedrock / vertex transport selector
    effort: Literal["low", "medium", "high"] | None = None
    # OPTIONAL, and `None` means "the operator did not say" — send no effort parameter at all.
    # It defaulted to "medium" until a shipped CHEAP target dropped its `effort: low` line to stop
    # the parameter being sent, and the default silently sent "medium" instead: a value nobody
    # wrote, transmitted as though it had been requested. There is no honest default for an
    # unstated preference, so absence is now representable. A backend MUST omit the parameter when
    # this is `None` rather than substituting one of its own.
    price: Price | Literal["free"] = Field(
        description="MANDATORY — no default, so an omitted price is a ValidationError at load "
        "rather than a fleet silently priced at $0.00 (§9 rule 5, §11.2). The loader surfaces "
        "it as exit 2 naming the profile, tier, and target index.",
    )
    capabilities_override: dict[str, object] = Field(default_factory=dict)
    weight: int = Field(default=100, ge=0)  # tie-break among healthy targets; 0 = standby only

    @model_validator(mode="after")
    def _free_is_declared_not_derived(self) -> BackendTarget:
        p = self.price
        if isinstance(p, Price) and not (p.in_per_mtok or p.out_per_mtok):
            raise ValueError("a zero-rate price must be declared as the literal `free` (§9 rule 5)")
        return self


class TransformTask(FleetModel):
    """One unit of Phase 2/3 work. Persisted to `tasks`."""

    task_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    repo_id: RepoId
    phase: Phase
    kind: TaskKind
    target_paths: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    dest_path: str
    max_attempts: int = Field(
        default=MAX_ATTEMPTS, ge=1, le=8,
        description="THE ceiling for this task's ladder; `le=8` is a sanity rail on config, not "
        "the ADR-0014 policy. Every attempt counter is checked against THIS, not a constant.",
    )
    pre_commit_sha: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{40}$",
        description="Tip of migrate/<repo> when this task was admitted — its per-task rollback "
        "anchor (§3.2 step 6). `git reset --hard` onto it undoes exactly this task, without "
        "disturbing a sibling task's commits or the phase-level `phases.base_ref`.",
    )
    ladder: tuple[ContextPolicy | None, ...] = Field(
        default=DEFAULT_LADDER,
        description="ADR-0021: context policy per rung, index == attempt - 1. len == max_attempts.",
    )
    token_budget: int = Field(
        default=200_000, ge=0, description="Hard cap for this task's LLM spend"
    )
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _ladder_matches_attempts(self) -> TransformTask:
        if len(self.ladder) != self.max_attempts:
            raise ValueError("ladder must declare exactly one context policy per attempt")
        if self.ladder[0] is not None:
            raise ValueError("attempt 1 is deterministic and may not declare a context policy")
        return self


class FilePatch(FleetModel):
    """One file's proposed edit. In-flight only: a `FilePatch` is the *input* to a commit, never a
    durable record of one. Once §3.2 step 6 commits, Git holds the change and this object is
    discarded — nothing persists `diff` to SQLite (ADR-0024)."""

    path: str
    diff: str = Field(description="Unified diff; the ONLY accepted patch representation")
    tier: TransformTier
    parse_probe_ok: bool
    rule_id: str | None = None


class RejectedApproach(FleetModel):
    """One approach already tried and refuted for this task (ADR-0021). This — NOT the transcript —
    is what a later rung is shown under EVIDENCE_PLUS_REJECTED_APPROACHES. There is deliberately
    no field capable of holding diff text, so a raw prior patch cannot travel inside it."""

    approach_signature: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="sha256 over sorted (path, change_kind, target_symbol) tuples; §3.2 step 5",
    )
    reason: str = Field(
        min_length=1,
        max_length=280,
        description="One line, approach level: what was tried and why it failed. No diff text.",
    )
    failure_class: FailureClass
    attempt: int = Field(ge=1)   # ceiling is the owning task's `max_attempts`, never a constant
    tier: TransformTier


class TransformResult(FleetModel):
    """Outcome of one TransformTask attempt."""

    task_id: UUID
    repo_id: RepoId
    attempt: int = Field(ge=1)   # ceiling is the owning task's `max_attempts`, never a constant
    revalidation_round: int = Field(
        default=0, ge=0,
        description="0 = first pass; N ≥ 1 = the Nth revalidation round (§3.5.1). Part of the "
        "§6 `attempts` primary key: without it a REVALIDATE re-run reuses 1..max_attempts and "
        "collides with the first-pass rows, making the build evidence unattributable.",
    )
    tier: TransformTier
    context_policy: ContextPolicy | None = Field(
        default=None, description="None iff tier == DETERMINISTIC (no prompt was rendered)"
    )
    ok: bool
    patches: list[FilePatch] = Field(default_factory=list)
    approach_signature: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="Fingerprint of THIS attempt's proposal, computed before `git apply --check`",
    )
    rejected_approaches: list[RejectedApproach] = Field(
        default_factory=list,
        description="Accumulated refutations carried into the next rung; never contains diffs",
    )
    anchored: bool = Field(
        default=False,
        description="True iff approach_signature collided with a rejected one (no probe was spent)",
    )
    reasks: int = Field(default=0, ge=0, description="In-rung anti-anchoring re-asks; not attempts")
    # ---- ADR-0024: Git is the code-state record; these two are POINTERS INTO it ----
    patch_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="Content-only idempotency key carried as the `Fleet-Patch-Id` commit trailer "
        "(§3.2 step 6). Independent of attempt number, so a re-run cannot double-apply.",
    )
    commit_sha: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{40}$",
        description="The commit this attempt produced on migrate/<repo>. A reference into Git, "
        "not a mirror of it: on disagreement Git is authoritative and this is corrected (§11.5).",
    )
    already_applied: bool = Field(
        default=False,
        description="True iff the pre-apply Git guard found this patch_id already on the branch "
        "(or `git apply --check --reverse` succeeded), so nothing was applied or committed",
    )
    files_changed: int = Field(default=0, ge=0)
    unresolved_files: list[str] = Field(default_factory=list)
    failure_class: FailureClass | None = None
    error: TruncatedStr | None = Field(
        default=None,
        description="Verbatim tool/probe output, tail-truncated (never rejected). Uncapped it "
        "put megabytes into SQLite × attempts × 250 repos; the whole stream is on disk at "
        "`artifacts/logs/<run_id>/<attempt_id>.log`.",
    )
    usage: TokenUsage = Field(default_factory=TokenUsage)
    duration_ms: int = Field(default=0, ge=0)
    finished_at: datetime = Field(default_factory=utcnow)


class BuildAttempt(FleetModel):
    """One sandboxed command execution. Persisted to `attempts`. The exit code IS the verdict —
    except for an ADR-0021 anchoring rejection, which executes nothing and is the one case where
    `exit_code` is None; `ok` is False there, so no verdict is ever inferred from an absence."""

    attempt_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    repo_id: RepoId
    phase: Phase
    attempt: int = Field(ge=1)   # ceiling is the owning task's `max_attempts`, never a constant
    revalidation_round: int = Field(
        default=0, ge=0,
        description="0 = first pass; N ≥ 1 = the Nth revalidation round (§3.5.1). Belongs in the "
        "§6 `attempts` PK alongside (run_id, repo_id, phase, attempt) — a REVALIDATE re-run "
        "reuses 1..max_attempts, so without it the second round overwrites the first's evidence.",
    )
    integration_ref: str | None = Field(
        default=None,
        description="The IMMUTABLE snapshot ref this build actually ran against — "
        "refs/fleet/integration/<n>, cut under the mutex (§3.3). A build attributed only to a "
        "moving branch name is unreproducible: the branch has advanced by the time it is read.",
    )
    command: list[str] = Field(default_factory=list)
    exit_code: int | None = Field(
        default=None,
        description="None ONLY when the rung was cut short before execution — an ANCHORED_REPEAT "
        "rejection spends no probe, so there is no exit code to record (ADR-0021)",
    )
    context_policy: ContextPolicy | None = Field(
        default=None, description="Context composition of the rung that produced this attempt"
    )
    approach_signature: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="Fingerprint of the proposal this attempt executed or rejected",
    )
    duration_ms: int = Field(ge=0)
    container_id: str | None = None
    worktree_path: str | None = None
    stdout_tail: TruncatedStr = ""
    stderr_tail: TruncatedStr = ""
    log_path: str | None = Field(
        default=None,
        description="`artifacts/logs/<run_id>/<attempt_id>.log` — the FULL stream. The tails "
        "above are a triage view; this path is the evidence a human reads.",
    )
    failure_class: FailureClass | None = None
    started_at: datetime
    finished_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _revalidation_is_a_verify_round(self) -> BuildAttempt:
        # A revalidation round exists only in Phase 4, and only downstream of a TaskKind.REVALIDATE
        # chain (§3.5.1). Anywhere else the counter is a mis-attributed first-pass build.
        if self.revalidation_round > 0 and self.phase is not Phase.VERIFY:
            raise ValueError("revalidation_round > 0 requires phase VERIFY, originating from "
                             "a TaskKind.REVALIDATE chain")
        return self

    @model_validator(mode="after")
    def _executed_iff_command(self) -> BuildAttempt:
        # A command was run iff there is an exit code. The only legal "neither" is an ADR-0021
        # anchoring rejection, which must say so rather than leave both fields silently empty.
        if bool(self.command) != (self.exit_code is not None):
            raise ValueError("command and exit_code must both be present or both be absent")
        if not self.command and self.failure_class is not FailureClass.ANCHORED_REPEAT:
            raise ValueError("an attempt that executed nothing must be ANCHORED_REPEAT")
        return self

    @property
    def ok(self) -> bool:
        return self.exit_code == 0  # None (nothing executed) is not success


class StubRecord(FleetModel):
    """One `stubs` row (§3.5.1) — **the authoritative stub lifecycle**, and the reason the
    lifecycle is not merely four prose transitions. `RepoState.stub_states`,
    `PullRequestDraft.unresolved_stub_states`, and `VerificationReport.stub_fidelity` are
    PROJECTIONS of this model, rebuilt from it by `stub_reconcile` and never written
    independently: three unreconciled denormalized copies disagree after a crash mid-supersede,
    and the first thing that reads a stale copy is `fleet pr --ready` (§12.37)."""

    stub_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    coord_key: str = Field(min_length=1, description="The provider's published coordinate key")
    provider_repo_id: RepoId = Field(description="The abandoned repo; never a contract (§3.5)")
    consumer_repo_ids: list[RepoId] = Field(
        default_factory=list, description="Every DEGRADED dependent bound to this stub label"
    )
    fidelity: StubFidelity
    pinned_version: str | None = Field(
        default=None, description="None ⇒ EMPTY_FAILING; a build-time failure, never a silent one"
    )
    state: StubState = StubState.ACTIVE
    max_revalidation_rounds: int = Field(
        default=2, ge=0, description="Per-consumer cap; mirrors stubs.max_revalidation_rounds (§9)"
    )
    rounds_spent: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utcnow)
    state_changed_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _fidelity_matches_pin(self) -> StubRecord:
        if self.fidelity is StubFidelity.PUBLISHED_ARTIFACT and self.pinned_version is None:
            raise ValueError(
                f"{self.coord_key}: PUBLISHED_ARTIFACT fidelity needs a pinned_version"
            )
        if self.rounds_spent > self.max_revalidation_rounds:
            raise ValueError(f"{self.coord_key}: rounds_spent exceeds its own cap")
        return self


class VerificationReport(FleetModel):
    """Phase 4 verdict, assembled from BuildAttempt rows only — never from model output."""

    run_id: UUID
    repo_id: RepoId
    build_ok: bool
    test_ok: bool
    rdeps_query: str = Field(default="", description="The exact bazel query executed")
    rdeps_target_count: int = Field(default=0, ge=0)
    rdeps_tested: int = Field(default=0, ge=0)
    rdeps_ok: bool = False
    rdeps_truncated: bool = Field(default=False, description="True → target count exceeded the cap")
    attempt_ids: list[UUID] = Field(
        default_factory=list, description="Evidence: rows in `attempts`"
    )
    verdict: Literal["PASS", "FAIL"] = "FAIL"
    verified_against_stubs: list[str] = Field(
        default_factory=list,
        description="coord_keys whose `stubs` row was ACTIVE for the whole of this verification "
        "(§3.5.1). Non-empty ⇒ this green does not prove what an unqualified green proves.",
    )
    stub_fidelity: dict[str, StubFidelity] = Field(
        default_factory=dict, description="coord_key → tier, for each entry above"
    )
    equivalence: Equivalence = Field(
        default=Equivalence.FULL,
        description="Derived, never supplied (see `_derive_equivalence`): STUB_LIMITED whenever "
        "verified_against_stubs is non-empty, else CLOSURE_SAMPLED whenever rdeps_truncated, "
        "else FULL. That precedence is fixed (EQUIVALENCE_RANK, §5.1).",
    )
    revalidation_round: int = Field(
        default=0, ge=0, description="0 = first-pass verification; N ≥ 1 = the Nth revalidation "
        "round after a stub was superseded (§3.5.1). Capped by stubs.max_revalidation_rounds.",
    )
    generated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="before")
    @classmethod
    def _derive_equivalence(cls, data: Any) -> Any:
        # A green build against a stub is NEVER reported as equivalent to a green build against
        # the real dependency (§3.5.1). Derived rather than accepted from a caller: that is what
        # stops an honest-looking PASS from being assembled by an optimistic one. It runs in
        # `mode="before"` and NOT as an assignment in `mode="after"`, because under
        # `validate_assignment=True` assigning to `self` inside an after-validator re-enters that
        # validator — a RecursionError on the first Phase 4 verdict.
        if isinstance(data, dict):
            data = dict(data)
            if data.get("verified_against_stubs"):
                data["equivalence"] = Equivalence.STUB_LIMITED
            elif data.get("rdeps_truncated"):
                data["equivalence"] = Equivalence.CLOSURE_SAMPLED   # a sampled closure is not FULL
            else:
                data["equivalence"] = Equivalence.FULL
        return data

    @model_validator(mode="after")
    def _fidelity_covers_every_stub(self) -> VerificationReport:
        if set(self.stub_fidelity) != set(self.verified_against_stubs):
            raise ValueError("stub_fidelity must name exactly the coord_keys in "
                             "verified_against_stubs")
        return self


class PullRequestDraft(FleetModel):
    """One stacked PR (ADR-0011). Opened only after every dependency PR is MERGED."""

    run_id: UUID
    repo_id: RepoId = Field(
        description="For a contract PR this is the OWNING repo, so the PR stays attributable "
        "to a code-owner without a contract needing a `phases` row (§3.3)"
    )
    contract_id: str | None = Field(
        default=None,
        description="Set iff this PR migrates a hoisted contract node (ADR-0019). It is a graph "
        "source, so stacking puts it ahead of its owner's and every consumer's PR.",
    )
    scc_id: SccId | None = Field(
        default=None, description="Set for an ATOMIC_WAVE SCC: one PR covers every member (§3.1 6e)"
    )
    member_repo_ids: list[RepoId] = Field(
        default_factory=list, description="SCC members when scc_id is set; else empty"
    )
    wave_index: int = Field(ge=0)
    branch: str = Field(pattern=r"^migrate/[a-z0-9._-]+$")
    base: str = Field(default="integration")
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1)
    depends_on_repos: list[RepoId] = Field(default_factory=list)
    depends_on_prs: list[str] = Field(default_factory=list)
    stubbed_deps: list[str] = Field(
        default_factory=list,
        description="coord_keys migrated against a stub (§3.5). "
        "Non-empty => DEGRADED => draft-only",
    )
    equivalence: Equivalence = Field(
        default=Equivalence.FULL,
        description="Mirrors the VerificationReport this body was rendered from. STUB_LIMITED "
        "puts the banner at the top of the body — the disclosure is data, not prose (§3.5.1).",
    )
    unresolved_stub_states: dict[str, StubState] = Field(
        default_factory=dict,
        description="coord_key → StubState for every stub row of this repo not yet RESOLVED. "
        "`fleet pr --ready` refuses while any value is ACTIVE or SUPERSEDED (§12.37).",
    )
    revalidation_round: int = Field(
        default=0, ge=0, description="Bumped each time this PR's branch is force-with-lease "
        "re-pushed and its body regenerated after a stub was superseded (§3.5.1). The PR number "
        "and url are invariant across rounds — a resolution never closes and re-opens a PR.",
    )
    weak_edges: list[EdgeKey] = Field(
        default_factory=list,
        description="edge_keys below min_confidence, surfaced for the reviewer",
    )
    source_url: str = Field(description="Credential-free; passed through obs/redact.py (§11.4)")
    source_sha: str
    state: PrState = PrState.DRAFTED
    url: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class LlmCallRecord(FleetModel):
    """One content-addressed LLM interaction. Persisted to `llm_cache`; the cache is what makes
    a re-run reproducible, since the harness pins no sampling controls (§11.6)."""

    cache_key: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="sha256(role|tier|backend|model_id|effort|context_policy|"
        "rejected_approach_digest|prompt_sha256|prompt_template_version|response_schema_sha256|"
        "adapter_versions) — ADR-0021: the policy and the refutation set are part of the identity "
        "of the call, not decoration; ADR-0023: so are the backend and the RESOLVED model id, or "
        "an answer from a local 8B would satisfy a call routed to a frontier model. "
        "`harness_version` is deliberately NOT a component: it changes on every patch release "
        "and would re-pay for every cached call across a fleet. What actually invalidates a "
        "cached answer is the prompt template or the response schema, and both are named here.",
    )
    prompt_template_version: int = Field(
        default=1, ge=1,
        description="Bumped by hand when a role's template changes meaning. This — not the "
        "harness version — is the invalidation knob for the cache.",
    )
    role: str = Field(
        min_length=1, description="Must exist in the active config/models.yaml profile"
    )
    tier: ModelTier = Field(
        description="ADR-0023: the tier the role resolved to under that profile"
    )
    backend: str = Field(min_length=1, description="Registered backend that produced the response")
    model_id: str = Field(
        min_length=1, description="Resolved model id — config data, never a literal"
    )
    structured_output_mode: StructuredOutputMode = Field(
        description="Which §7.7 rung produced this response. A PROMPTED result from a tier whose "
        "profile promised JSON_SCHEMA is a capability-drift finding, not a silent success."
    )
    effort: Literal["low", "medium", "high"] | None = Field(
        default=None,
        description="None when the target declared none — recorded as absent, never as the "
        "value a default would have invented, because this column is a cache-key component.",
    )
    context_policy: ContextPolicy | None = Field(
        default=None, description="None only for non-ladder roles that compose no prior context"
    )
    rejected_approach_digest: str = Field(
        default="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        pattern=r"^[0-9a-f]{64}$",
        description="sha256 over the sorted approach_signatures actually rendered into the prompt; "
        "sha256(b'') when none were. Distinguishes a fresh-slate call from a primed one.",
    )
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_json: str = Field(description="Validated response, verbatim; redacted before write")
    usage: TokenUsage = Field(default_factory=TokenUsage)
    hit_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utcnow)
