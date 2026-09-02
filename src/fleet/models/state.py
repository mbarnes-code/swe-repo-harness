"""The `migration_state.json` projection (SPEC §5.5)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field, computed_field, model_validator

from fleet.models.base import FleetModel, TruncatedStr, utcnow
from fleet.models.enums import FailureClass, Phase, RepoStatus, StubState
from fleet.models.graph import CollisionFinding, ContractNode, CycleFinding, MigrationWave, SccId
from fleet.models.repo import RepoId
from fleet.models.tasks import MAX_ATTEMPTS, TokenUsage

SCHEMA_VERSION = 9   # == PRAGMA user_version (§6): 2 ADR-0019, 3 ADR-0021, 4 ADR-0022,
                     #                           5 ADR-0023 (llm_cache backend identity),
                     #                           6 ADR-0024 (mutations dropped; Git owns code state)
                     #                           7 edge_key/scc_id logical PKs, revalidation_round
                     #                             in the attempts PK, leases, StubRecord
                     #                           8 `reservations`: per-holder budget identity, so
                     #                             the reaper releases the DEAD worker's hold and
                     #                             not the live ones' (§6 RESERVATION ACCOUNTING)
                     #                           9 `coordinates.version`: the owning repo's own
                     #                             published version, already computed by 4 of 5
                     #                             ecosystem adapters and no longer discarded
                     #                             before reaching durable storage (§37 Blocker B)
# Every version has a forward-only `src/fleet/migrations/vNNN_<slug>.py` exposing
# `upgrade(conn: sqlite3.Connection) -> None`; this batch ships `v007_logical_keys.py`. `fleet
# migrate-db` (§10) — the ONLY DDL path; `fleet migrate` is the unrelated repo-migration verb —
# applies each pending file in order inside one transaction and sets `PRAGMA user_version`.
# A schema bump is therefore NOT a reason to discard an in-flight run:
# a 250-repo fleet is days of LLM spend, and "resume across versions is refused" would make
# every harness upgrade destroy it mid-Phase-3.


class PhaseRecord(FleetModel):
    """Authoritative row shape of the `phases` table, keyed (run_id, repo_id, phase)."""

    phase: Phase
    status: RepoStatus = RepoStatus.PENDING
    attempts: int = Field(default=0, ge=0, description="Substantive attempts only; the ceiling is "
                          "the owning TransformTask.max_attempts, checked at runtime")
    max_attempts: int = Field(
        default=MAX_ATTEMPTS, ge=1, le=8,
        description="Copied from the owning task when the phase is admitted, so `exhausted()` "
        "and a resume both read the ladder length THIS phase actually ran under — not whatever "
        "the constant happens to be after an upgrade.",
    )
    transient_retries: int = Field(
        default=0, ge=0, description="Backed-off infra retries; not attempts"
    )
    failure_class: FailureClass | None = None
    last_error: TruncatedStr | None = Field(
        default=None, description="Redacted by obs/redact.py before it is ever written (§11.4), "
        "and tail-truncated rather than length-rejected (see TruncatedStr)"
    )
    # ---- liveness (§11.5). A reaper that resets a status without checking the lease produces
    # TWO writers on migrate/<repo>: the reset repo is re-admitted while the original container
    # still holds the worktree. The lease, not the heartbeat, is what makes that impossible. ----
    heartbeat_at: datetime | None = Field(
        default=None,
        description="Last liveness stamp from utcnow() on the orchestrator host. Stale means "
        "`utcnow() - heartbeat_at > heartbeat_ttl_seconds`; there is no other definition.",
    )
    heartbeat_ttl_seconds: int = Field(
        default=300, gt=0,
        description="Config-sourced (`orchestrator.stale_after_s`, §9), captured per phase so a "
        "config change cannot retroactively declare a live worker dead.",
    )
    lease_owner: str | None = Field(
        default=None,
        description="Opaque worker identity holding the worktree. Its FORMAT is declared in "
        "exactly one place — §6 `phases.lease_owner` — and is deliberately not restated here, so "
        "the two cannot drift; the short version of why it is not a bare pid is that fresh PID "
        "namespaces reuse low pids and two containers would collide. A write whose lease_owner "
        "does not match the stored one is REJECTED, not merged — that is the single-writer "
        "guarantee (§11.5), and it holds even if the reaper is wrong.",
    )
    lease_fence: int = Field(
        default=0, ge=0,
        description="§6 `phases.lease_fence`: monotonic, bumped by the reaper in the same "
        "transaction that reclaims the lease. Every mutating statement a worker issues against "
        "its own phase row carries `AND lease_fence = ?` with the fence it was granted at claim "
        "time; a `rowcount == 0` means the lease was STOLEN, and that worker MUST abort "
        "immediately — WITHOUT touching git or the worktree — and emit `LeaseStolen`. Without "
        "this field the model cannot round-trip its own row, and a reclaimed lease has no way to "
        "invalidate the old holder's writes (§11.5).",
    )
    lease_expires_at: datetime | None = Field(
        default=None,
        description="Reclaimable only after this instant AND a stale heartbeat. The reaper "
        "clears the lease and the worktree in the same transaction as the RUNNING → PENDING "
        "transition, so the two can never disagree.",
    )
    started_at: datetime | None = None
    # ---- resume contract (§11.5), ADR-0024: POINTERS into Git, never a copy of its content ----
    base_ref: str | None = Field(
        default=None,
        description="Known-good anchor, a REAL git ref: "
        "refs/fleet/<run_id>/<repo_id>/phase-<n>/base. "
        "Rollback is `git reset --hard` onto it; nothing is reconstructed from a stored diff.",
    )
    pre_commit_sha: str | None = Field(
        default=None, description="Branch tip the anchor ref names, cached for cheap comparison"
    )
    post_commit_sha: str | None = Field(
        default=None,
        description="Newest commit this phase produced. Cached pointer: if it disagrees with the "
        "Fleet-Phase trailer walk on migrate/<repo>, Git wins and this column is corrected.",
    )
    updated_at: datetime = Field(default_factory=utcnow)

    def exhausted(self, max_attempts: int | None = None) -> bool:
        """Compares against the ladder THIS phase runs under, never against a module constant:
        the ladder is per-run configurable (§9), so a compiled-in ceiling would silently make
        that promise false. A method rather than a property because the answer depends on the
        owning task, which the row alone does not know until `max_attempts` is stamped on it."""
        return self.attempts >= (max_attempts if max_attempts is not None else self.max_attempts)

    def is_stale(self, now: datetime) -> bool:
        """`now` is always `utcnow()` from the orchestrator host (§11.5)."""
        if self.heartbeat_at is None:
            return False
        return (now - self.heartbeat_at).total_seconds() > self.heartbeat_ttl_seconds


class RepoState(FleetModel):
    """One entry under `migration_state.json#repos`. Field set is fixed by ADR-0012."""

    phase: Phase = Phase.SCAN
    status: RepoStatus = RepoStatus.PENDING
    attempts: int = Field(default=0, ge=0, description="Ceiling is the owning task's max_attempts")
    last_error: TruncatedStr | None = None
    blocked_by: list[RepoId] = Field(
        default_factory=list,
        description="Set union of transitive ancestors over the ordering subgraph appended by the "
        "§3.5 propagation rule — NOT the ancestors of one status. The triggers reaching that rule "
        "are enumerated here rather than counted, because a stated cardinal has been wrong in both "
        "versions of this sentence that stated one (`9b34497` and `a69fba8`; measured, they are "
        "the only two): RepoStatus.REQUIRES_HUMAN_INTERVENTION (§3.5) and `fleet quarantine`, "
        "which writes SKIPPED (§10), are LIVE — measured at `34d6f82`, runner._contain and "
        "cli._quarantine_impl are the only non-delegating callers in `src/`. Non-delegating means "
        "it calls the write sink `SqliteSchedulerStore.append_blocked_by`, or the removal "
        "sink `SqliteStateRepository.clear_blocked_by`, or the "
        "`WaveScheduler.propagate_blocked` rule that fronts it, and is itself neither of those two "
        "nor a same-named wrapper forwarding to an inner store; under the bare reading 'callers of "
        "the append' the named pair is wrong in both directions, so "
        "`tests/test_blocked_by_writer_statements.py` derives the set from the AST and fails "
        "either way. An SCC's failed members (§3.1), a `pr.merge_wait_timeout_s` breach (§3.4) and "
        "a failed contract's non-terminal descendants (§3.5) — SPEC §3.5 routes "
        "`contracts.status='FAILED'` through the same propagation rule — are SPEC-mandated with 0 "
        "producers today, so a recompute must not treat the writer set as closed. A recompute "
        "defined as 'the RHI ancestors' therefore EMPTIES the `blocked_by` of every quarantined "
        "repo's dependents and re-admits them, undoing an audited OperatorQuarantine on each "
        "`fleet resume`. 'abandoned' is prose, not a status: RepoStatus has no ABANDONED member "
        "and StubState.ABANDONED is a different machine, so the word names nothing this field "
        "holds. For that contract case, SPEC §3.5 additionally MANDATES a `contract_id` in the SQL "
        "column `phases.blocked_by` on `contracts.status='FAILED'`; measured at `31484d5`, no code "
        "writes one (0 producers) and this `list[RepoId]` annotation would reject it, so the "
        "column and this field are not interchangeable and this is not a licence to widen either. "
        "REMOVAL IS A SEPARATE CLASS FROM THE APPEND AND IS ENUMERATED SEPARATELY, because an "
        "un-blocking is not the §3.5 propagation rule run backwards: the append marks the row "
        "BLOCKED and the recompute only ever returns one to PENDING. The only non-delegating "
        "remover in `src/` is cli._apply_unblocking, driving "
        "`SqliteStateRepository.clear_blocked_by` for §11.5 step 6; what it may remove is decided "
        "by orchestrator.reentry.plan_unblocking and nothing else, at the "
        "retain-what-cannot-be-resolved polarity ADR-0090 §2.4 rules R2-CLOSED. Naming a remover "
        "in the trigger enumeration above would be a category error and is refused by "
        "`tests/test_blocked_by_writer_statements.py`. Three defects of that predicate were "
        "open at `f4eade0` and are CLOSED by the reshape shipping with this sentence: it was "
        "a deny-list that removed a blocker projected PENDING, RUNNING, BLOCKED or DEGRADED, "
        "none of which has landed, and is now a whitelist of landed statuses, so an "
        "unrecognised or newly added status is retained by default; BlockerState carried one "
        "status for a quantity that is per (repo, phase) and now carries the whole set of a "
        "blocker's phase statuses, every member of which a removal requires to be landed, so "
        "no caller-side reduction can lose the row that blocks; and the between-phases "
        "silent undo those two produced is therefore closed at the predicate and not only at "
        "`cli._blocker_states`, which now groups rows and no longer reduces. Reversible "
        "(§3.5, §12.14).",
    )
    depends_on: list[RepoId] = Field(default_factory=list)
    depends_on_contracts: list[str] = Field(
        default_factory=list,
        description="contract_ids this repo consumes or owns; each is migrated in an earlier wave",
    )
    blast_radius: int = Field(default=0, ge=0, description="|transitive dependents| (§3.5)")
    stubbed_deps: list[str] = Field(
        default_factory=list, description="coord_keys stubbed; non-empty ⇒ status DEGRADED"
    )
    stub_states: dict[str, StubState] = Field(
        default_factory=dict,
        description="coord_key → lifecycle state, one entry per `stubs` row (§3.5.1). A repo is "
        "eligible to leave DEGRADED only when every value is RESOLVED.",
    )
    revalidation_rounds: int = Field(
        default=0, ge=0, description="Rounds spent re-verifying after a stub was superseded; "
        "capped by stubs.max_revalidation_rounds. Does NOT contribute to `attempts` (§3.5.1).",
    )
    revalidation_usd: float = Field(
        default=0.0, ge=0.0, description="Mirrors repo_ledger.revalidation_usd — the rework is "
        "priced, not free. A sub-ceiling inside repo_max_cost_usd (§11.2).",
    )
    scc_id: SccId | None = Field(default=None, description="Set iff in an ATOMIC_WAVE SCC")
    pr_url: str | None = None
    wave_index: int | None = Field(default=None, ge=0)
    dest_path: str | None = None
    updated_at: datetime = Field(default_factory=utcnow)
    phases: dict[Phase, PhaseRecord] = Field(default_factory=dict, description="Per-phase detail")

    @model_validator(mode="after")
    def _stub_invariants(self) -> RepoState:
        # Prose invariants are not invariants. Without this, RepoState(status=SUCCEEDED,
        # stubbed_deps=[...], stub_states={...: ACTIVE}) validates cleanly — and a repo ships as
        # SUCCEEDED with unresolved stubs, which is exactly what §3.5.1 exists to prevent.
        if bool(self.stubbed_deps) != (self.status is RepoStatus.DEGRADED):
            raise ValueError("stubbed_deps is non-empty iff status is DEGRADED (§3.5)")
        if set(self.stub_states) != set(self.stubbed_deps):
            raise ValueError("stub_states must name exactly the coord_keys in stubbed_deps")
        # SUCCEEDED-with-an-unresolved-stub is unreachable BY CONSTRUCTION from the two checks
        # above: promotion out of DEGRADED clears the projection, and it clears only once every
        # StubRecord for the repo is RESOLVED (§3.5.1). The audit trail lives on StubRecord,
        # which is why the projection is safe to clear.
        return self


class MigrationState(FleetModel):
    """The `migration_state.json` projection (CLAUDE.md Rule 6/11, ADR-0012).

    Derived from SQLite and written atomically (temp file + os.replace) after every state
    transition. SQLite is authoritative on any conflict; this file is never written to
    directly and never read as the source of truth on resume.

    This is the ONLY name for this model (ADR-0012, corrected). There is no `FleetState` alias.
    """

    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    run_id: UUID
    started_at: datetime
    updated_at: datetime = Field(default_factory=utcnow)
    monorepo_branch: str = "integration"
    config_sha256: str = Field(
        default="", description="Mirrors runs.config_sha256; a mismatch on resume is a hard stop"
    )
    harness_version: str = Field(
        default="",
        description="Semver. Resume compares only the MAJOR component: a patch or minor upgrade "
        "mid-run is expected and permitted, and any schema delta it carries is applied by "
        "`fleet migrate-db` (see SCHEMA_VERSION). Only a MAJOR bump — the one that declares a "
        "semantic break — refuses resume, because only that one cannot be migrated forward.",
    )
    repos: dict[RepoId, RepoState] = Field(default_factory=dict)
    contracts: list[ContractNode] = Field(
        default_factory=list,
        description="Hoisted contract nodes only (status HOISTED/MIGRATED/FAILED); the operator's "
        "third triage list, and the reason a wave can contain a non-repo (ADR-0019)",
    )
    waves: list[MigrationWave] = Field(default_factory=list)
    cycles: list[CycleFinding] = Field(default_factory=list)
    collisions: list[CollisionFinding] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    budget_remaining_usd: float = Field(
        default=0.0, description="run_max_cost_usd minus the durable ledger; fail-closed at <= 0"
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def counts(self) -> dict[str, int]:
        out = {s.value: 0 for s in RepoStatus}
        for r in self.repos.values():
            out[r.status.value] += 1
        return out

    @computed_field  # type: ignore[prop-decorator]
    @property
    def needs_human(self) -> list[RepoId]:
        return sorted(
            k for k, v in self.repos.items()
            if v.status is RepoStatus.REQUIRES_HUMAN_INTERVENTION
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def degraded(self) -> list[RepoId]:
        """Repos migrated against a stub (§3.5). The operator's second triage list."""
        return sorted(k for k, v in self.repos.items() if v.status is RepoStatus.DEGRADED)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def unresolved_stubs(self) -> dict[RepoId, list[str]]:
        """The "Degraded and unresolved" section of the final report (§3.5.1 reconciliation):
        consumer → coord_keys whose stub never reached RESOLVED. Non-empty at end of run means
        the fleet has work it must NOT ship, and the run exits 7."""
        return {
            k: sorted(c for c, st in v.stub_states.items() if st is not StubState.RESOLVED)
            for k, v in sorted(self.repos.items())
            if any(st is not StubState.RESOLVED for st in v.stub_states.values())
        }
