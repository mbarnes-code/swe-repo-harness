"""Closed vocabularies and the status state machine (SPEC §5.1)."""

from enum import IntEnum, StrEnum


class Phase(IntEnum):
    SCAN = 1        # Fleet Scanner & Topological Sequencer
    TRANSFORM = 2   # Hybrid Transformation Engine
    BUILD = 3       # Monorepo Build System Integration
    VERIFY = 4      # Verification & PR Generator


class RepoStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    BLOCKED = "BLOCKED"                                    # a dependency was abandoned
    DEGRADED = "DEGRADED"                                  # migrated against a stub (§3.5)
    REQUIRES_HUMAN_INTERVENTION = "REQUIRES_HUMAN_INTERVENTION"  # terminal (ADR-0014)
    SKIPPED = "SKIPPED"                                    # excluded by config


TERMINAL_STATUSES = frozenset(
    {RepoStatus.SUCCEEDED, RepoStatus.REQUIRES_HUMAN_INTERVENTION, RepoStatus.SKIPPED}
)  # BLOCKED and DEGRADED are deliberately NOT terminal: both are RESOLVABLE (§3.5.1) — which is
# not the same as free. DEGRADED leaves the machine only via a budgeted revalidation round; a
# DEGRADED repo at end of run is reconciled, held as a draft PR, and exits 7 (§3.5.1).


ALLOWED_TRANSITIONS: dict[RepoStatus, frozenset[RepoStatus]] = {
    RepoStatus.PENDING: frozenset(
        {RepoStatus.RUNNING, RepoStatus.BLOCKED, RepoStatus.SKIPPED}
    ),
    RepoStatus.RUNNING: frozenset(
        {RepoStatus.PENDING,        # crash sweep only: stale lease, `attempts` retained (§11.5)
         RepoStatus.SUCCEEDED, RepoStatus.DEGRADED, RepoStatus.BLOCKED,
         RepoStatus.REQUIRES_HUMAN_INTERVENTION}
    ),
    RepoStatus.BLOCKED: frozenset({RepoStatus.PENDING, RepoStatus.SKIPPED}),
    RepoStatus.DEGRADED: frozenset(
        {RepoStatus.RUNNING,                      # a budgeted revalidation round (§3.5.1)
         RepoStatus.SUCCEEDED,                    # every `stubs` row reached RESOLVED
         RepoStatus.REQUIRES_HUMAN_INTERVENTION}  # stub rot (§13 row 33)
    ),
    RepoStatus.SUCCEEDED: frozenset(),
    RepoStatus.REQUIRES_HUMAN_INTERVENTION: frozenset(),
    RepoStatus.SKIPPED: frozenset(),
}  # Terminal statuses map to the EMPTY set, which is what makes them terminal mechanically
# rather than by prose: no crash sweep can resurrect an abandoned repo into RUNNING.


OPERATOR_REOPEN: dict[RepoStatus, frozenset[RepoStatus]] = {
    RepoStatus.REQUIRES_HUMAN_INTERVENTION: frozenset({RepoStatus.PENDING}),
}  # The one documented exception (§12.14): a human re-runs an abandoned repo via `fleet retry`.
# Explicit, audited to `findings`, and unreachable from any automatic path — which is precisely
# the difference between "the operator un-abandoned it" and "the reaper lost track of it".


def transition(old: RepoStatus, new: RepoStatus, *, operator: bool = False) -> RepoStatus:
    """THE single gate for every status write (§6, §11.5). A no-op re-write of the same status
    is allowed, so an idempotent replay (§11.7) is not an error; anything unlisted raises."""
    if new is old:
        return new
    if new in ALLOWED_TRANSITIONS[old]:
        return new
    if operator and new in OPERATOR_REOPEN.get(old, frozenset()):
        return new
    raise ValueError(f"illegal status transition {old.value} -> {new.value}")


class StubState(StrEnum):
    """Lifecycle of one `stubs` row (§3.5.1). Four states, four transitions, no state without
    an inbound transition. RESOLVED and ABANDONED are terminal."""

    ACTIVE = "ACTIVE"            # emitted; consumer verified against it; provider still abandoned
    SUPERSEDED = "SUPERSEDED"    # provider fixed + PR MERGED; label swapped; revalidation pending
    RESOLVED = "RESOLVED"        # a revalidation round PASSed against the real dependency
    ABANDONED = "ABANDONED"      # rot, rounds/budget exhausted, end-of-run, or operator abandon


class StubFidelity(StrEnum):
    """How big the lie is (§3.5.1). Exactly two tiers, because the harness emits exactly two."""

    PUBLISHED_ARTIFACT = "PUBLISHED_ARTIFACT"  # r's last release: signature- AND behaviour-honest
                                               #   for `pinned_version`, and only for it
    EMPTY_FAILING = "EMPTY_FAILING"            # r never released: fails at build time, loudly.
                                               #   Never yields a DEGRADED consumer (§3.5 item 2)


class Equivalence(StrEnum):
    """Whether a green verdict may be read as equivalent to a green build against the real
    dependency graph. Set by a validator on `VerificationReport`, never by a caller (§3.5.1)."""

    FULL = "FULL"
    CLOSURE_SAMPLED = "CLOSURE_SAMPLED"  # real deps, but the rdeps closure was capped (§3.4)
    STUB_LIMITED = "STUB_LIMITED"        # at least one dep was a stub — the more serious limit


EQUIVALENCE_RANK: dict[Equivalence, int] = {   # fixed precedence, worst first (§3.5.1)
    Equivalence.STUB_LIMITED: 2,
    Equivalence.CLOSURE_SAMPLED: 1,
    Equivalence.FULL: 0,
}  # A stub-limited report over a sampled closure is limited in the more serious way, so
# STUB_LIMITED wins; `rdeps_truncated` can only ever pull FULL down to CLOSURE_SAMPLED.


class Ecosystem(StrEnum):
    """Hand-maintained, deliberately (§1): the values are persisted in `CHECK` constraints (§6)
    and embedded in `Coordinate.key` (ADR-0017), so a synthesized enum would defeat `mypy
    --strict` and make the DDL unverifiable. Adding a member without adding the matching
    `EcosystemAdapter` (§7.5) is a startup error, not a Phase 3 crash — `ecosystems.discover()`
    asserts a total bijection between this enum and the adapter registry (ADR-0020, §13 row 30).
    """

    MAVEN = "maven"
    GRADLE = "gradle"
    NPM = "npm"
    PYPI = "pypi"
    GO = "go"
    CARGO = "cargo"
    UNKNOWN = "unknown"   # no manifest / no adapter matched; §3.1 step 2 fallback path


class NodeKind(StrEnum):
    """A DAG node is `(kind, id)` — not always a repo (ADR-0019, §3.1 step 5b)."""

    REPO = "REPO"          # id is a repos.repo_id
    CONTRACT = "CONTRACT"  # id is a contracts.contract_id, '{kind}:{identifier}' case-folded


class ContractKind(StrEnum):
    """Declared shared-interface artifacts. Bounded on purpose: no per-symbol node exists."""

    PROTO = "PROTO"            # a protobuf `package`
    OPENAPI = "OPENAPI"        # one document with an `openapi:`/`swagger:` root
    AVRO = "AVRO"              # an .avsc/.avdl `namespace`
    THRIFT = "THRIFT"          # a .thrift `namespace`
    SHARED_LIB = "SHARED_LIB"  # explicitly declared only; never auto-discovered


class ContractStatus(StrEnum):
    """Lifecycle of a contract node; a contract has no `phases` row (§3.3)."""

    DETECTED = "DETECTED"        # discovered, candidacy not yet decided
    EXTRACTABLE = "EXTRACTABLE"  # passed every §3.1 5b (vi) predicate
    REJECTED = "REJECTED"        # failed one; the predicate is appended as 'REJECTED:<name>'
    FORBIDDEN = "FORBIDDEN"      # operator veto: --forbid-hoist
    HOISTED = "HOISTED"          # committed in 6c-H; retargets applied, node in the DAG
    MIGRATED = "MIGRATED"        # its wave built green in the monorepo
    FAILED = "FAILED"            # hoist attempted and rolled back (§3.1 6c-H failure path)


class EdgeKind(StrEnum):
    DECLARED_DEP = "DECLARED_DEP"              # manifest-declared, resolves to an internal repo
    PUBLISHED_ARTIFACT = "PUBLISHED_ARTIFACT"  # pinned published artifact of an internal repo
    INTERNAL_IMPORT = "INTERNAL_IMPORT"        # import/require/use with no manifest entry
    API_CONTRACT = "API_CONTRACT"              # OpenAPI operationId / protobuf / gRPC service FQN
    CONTRACT_IMPL = "CONTRACT_IMPL"            # owning repo -> its own hoisted contract node
    CONTRACT_CONSUME = "CONTRACT_CONSUME"      # consumer repo (or contract) -> a contract node
    SHARED_RESOURCE = "SHARED_RESOURCE"        # shared DB table / topic / queue (advisory)
    DYNAMIC_REF = "DYNAMIC_REF"                # reflection / dynamic import / string-built name


CONTRACT_EDGE_KINDS: frozenset[EdgeKind] = frozenset(
    {EdgeKind.CONTRACT_IMPL, EdgeKind.CONTRACT_CONSUME}
)  # dst_kind is always NodeKind.CONTRACT for these two, and only for these two.


KIND_RANK: dict[EdgeKind, int] = {         # cycle-break cost ordering, §3.1 step 6c
    EdgeKind.DYNAMIC_REF: 0,
    EdgeKind.SHARED_RESOURCE: 0,
    EdgeKind.API_CONTRACT: 1,
    EdgeKind.INTERNAL_IMPORT: 2,
    EdgeKind.PUBLISHED_ARTIFACT: 3,
    EdgeKind.DECLARED_DEP: 4,
    EdgeKind.CONTRACT_CONSUME: 5,          # ranked ABOVE DECLARED_DEP: breaking one would
    EdgeKind.CONTRACT_IMPL: 6,             #   undo the hoist that produced it (§3.1 6c-H)
}


class BreakStrategy(StrEnum):
    """How a non-trivial SCC was resolved (§3.1 step 6)."""

    CONTRACT_HOIST = "CONTRACT_HOIST"  # dissolved by hoisting contracts alone; no edge broken
    EDGE_BREAK = "EDGE_BREAK"    # feedback edges suppressed; members sequenced normally
    ATOMIC_WAVE = "ATOMIC_WAVE"  # SCC migrates as one indivisible unit, one PR
    MANUAL = "MANUAL"            # over scc_hard_max, or --break-cycles manual


class SymbolKind(StrEnum):
    CLASS = "class"
    FUNCTION = "function"
    INTERFACE = "interface"
    MODULE = "module"
    GRPC_SERVICE = "grpc_service"
    PROTO_MESSAGE = "proto_message"
    HTTP_OPERATION = "http_operation"
    DB_TABLE = "db_table"
    QUEUE_TOPIC = "queue_topic"
    IMPORT = "import"
    DYNAMIC_REF = "dynamic_ref"   # Class.forName / importlib / require(expr) / DI string key


class TaskKind(StrEnum):
    # relocate + ingest one contract node (§3.3 step 1); carries contract_id
    HOIST = "HOIST"
    RELOCATE = "RELOCATE"
    REWRITE = "REWRITE"
    BUILDGEN = "BUILDGEN"
    BUILD_VERIFY = "BUILD_VERIFY"
    RDEP_VERIFY = "RDEP_VERIFY"
    PR_EMIT = "PR_EMIT"
    REVALIDATE = "REVALIDATE"  # §3.5.1: re-verify a DEGRADED consumer after its stub is superseded
                               #   carries `tasks.revalidation_key`; never a HOIST/contract task


class ModelTier(StrEnum):
    """Capability tier a role is routed to (ADR-0023). Deliberately carries NO vendor string:
    a profile is free to point every tier at one locally-served model."""

    HEAVY = "HEAVY"          # cross-repo semantics; runs a few dozen times and must be right
    WORKHORSE = "WORKHORSE"  # per-file repair, build diagnosis, prose; the bulk of the spend
    CHEAP = "CHEAP"          # classification/labelling; runs ~250 × N times and must be cheap


class StructuredOutputMode(StrEnum):
    """Rungs of the §7.7 capability-negotiation ladder, best first. Recorded on every
    `LlmCallRecord` so a schema failure is attributable to the rung that produced it."""

    JSON_SCHEMA = "JSON_SCHEMA"      # backend accepts our JSON Schema and enforces it natively
    TOOL_CALL = "TOOL_CALL"          # schema smuggled as a single mandatory tool's parameters
    CONSTRAINED = "CONSTRAINED"      # server-side constrained decoding (e.g. vLLM guided JSON)
    PROMPTED = "PROMPTED"            # schema in the prompt; parse-and-repair is the only guard


class TransformTier(StrEnum):
    """Which rung of the ADR-0014 ladder actually produced the result."""

    DETERMINISTIC = "DETERMINISTIC"
    LLM_REPAIR = "LLM_REPAIR"        # attempt 2, role `transform_repair` → WORKHORSE tier
    LLM_ESCALATION = "LLM_ESCALATION"  # attempt 3, role `escalation` → HEAVY tier


class ContextPolicy(StrEnum):
    """What a ladder rung's prompt is allowed to contain (ADR-0021, §3.2 step 5).

    Evidence — failure class, failing probe, verbatim stderr, unresolved symbols, the target
    file's current content, relocation map, dependency context — is carried by EVERY policy.
    What varies is how much of the PRIOR PROPOSAL travels with it.
    """

    EVIDENCE_ONLY = "EVIDENCE_ONLY"                       # fresh slate; no prior proposal at all
    EVIDENCE_PLUS_REJECTED_APPROACHES = "EVIDENCE_PLUS_REJECTED_APPROACHES"  # + summaries, no diffs
    EVIDENCE_PLUS_PRIORS = "EVIDENCE_PLUS_PRIORS"         # + raw prior diffs; opt-in, never default


class ApproachChangeKind(StrEnum):
    """Closed vocabulary for one hunk's change, assigned by ast-grep in `rewrite/approach.py`.
    An input to `approach_signature` — never model-assigned (§3.2 step 5)."""

    IMPORT_REWRITE = "IMPORT_REWRITE"
    PACKAGE_DECL = "PACKAGE_DECL"
    PATH_ALIAS = "PATH_ALIAS"
    SYMBOL_RENAME = "SYMBOL_RENAME"
    DEP_ADD = "DEP_ADD"
    DEP_REMOVE = "DEP_REMOVE"
    FILE_ADD = "FILE_ADD"
    FILE_DELETE = "FILE_DELETE"
    OTHER = "OTHER"


class FailureClass(StrEnum):
    PARSE_ERROR = "PARSE_ERROR"
    RULE_MISS = "RULE_MISS"
    PATCH_REJECTED = "PATCH_REJECTED"
    ANCHORED_REPEAT = "ANCHORED_REPEAT"  # proposal re-fingerprints a rejected approach (ADR-0021)
    BUILD_ERROR = "BUILD_ERROR"
    TEST_FAILURE = "TEST_FAILURE"
    DEP_CONFLICT = "DEP_CONFLICT"
    CYCLE = "CYCLE"
    TIMEOUT = "TIMEOUT"
    STUB_DIVERGED = "STUB_DIVERGED"      # stub rot: the real target's surface is incompatible with
                                         #   `pinned_version`. Differential-detected, never guessed
                                         #   (§3.5.1); goes straight to human, not to the ladder
    COLLISION = "COLLISION"              # unresolved `collisions` row (§3.1 step 8)
    PREFLIGHT = "PREFLIGHT"              # git preflight gate (§3.1 step 1)
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"  # token/cost/wall-clock ceiling; fail-closed (§11.2)
    TRANSIENT_INFRA = "TRANSIENT_INFRA"  # never increments `attempts` (ADR-0014)
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"  # every target for a tier is DOWN (ADR-0023).
                                         #   Like TRANSIENT_INFRA it never increments `attempts` —
                                         #   the infrastructure failed, not the repo — but unlike
                                         #   it, it is terminal for the RUN: fail closed, exit 8
                                         #   (§11.8). Repos stay PENDING for `fleet resume`.
    DISK_EXHAUSTED = "DISK_EXHAUSTED"    # `budgets.max_disk_gb` / `preflight.min_free_bytes`
                                         #   breached after cache eviction (§11.3, §13 row 42).
                                         #   Detected BEFORE the write that would fail, so the
                                         #   checkpoint is still writable; terminal, exit 9
    UNKNOWN = "UNKNOWN"                  # unclassified worker exception (§11.1); never silent —
                                         #   `last_error` carries the verbatim text


class PrState(StrEnum):
    DRAFTED = "DRAFTED"
    OPEN = "OPEN"
    MERGED = "MERGED"
    CLOSED = "CLOSED"
    HELD = "HELD"      # §3.5.1 end-of-run reconciliation: still a draft on the forge, and the
                       #   harness has finished without resolving its stubs, so it will never be
                       #   promoted by this run. Entered ONLY from DRAFTED, by `stub_reconcile`.
                       #   Distinct from DRAFTED, which merely awaits the ADR-0011 stacking gate.
