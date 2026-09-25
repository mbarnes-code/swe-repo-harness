"""ADR-0023: the `ModelClient` protocol + `ModelBackend` registry (SPEC §7.7).

The ONLY module the rest of the harness imports to talk to a model. No vendor SDK, no
framework, no `base_url`, no model string crosses this line — a backend adapter is *injected*
(CLAUDE.md guardrail 3), which is the whole reason the harness is model-agnostic.

The protocols and payload models below are SPEC §7.7 verbatim. `LadderModelClient` is the
reference implementation of `ModelClient`: role → tier → ordered `BackendTarget`s, capability
negotiation, budget re-check per target, same-target RATE_LIMIT backoff, truncation retry,
budgeted repair, health-gated failover, and progress-only streaming. **`llm/failover.py`'s
`BackendHealth` (ADR-0132) is the first of §8's eventual split to land** — the per-target §11.8
circuit-breaker STATE lives there; `complete()` here still owns the dispatch loop that consults
it. Routing and negotiation remain future splits: the pure functions here (`negotiate`,
`promised_mode`, `estimate_cost_usd`) are the seams `routing.py`/`negotiate.py` would take over,
and `RoleRouter` is already a Protocol so `routing.py` plugs in without this file changing.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import pkgutil
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from importlib import import_module
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field, ValidationError, model_validator

from fleet.llm.failover import BackendHealth, BackendHealthTransition
from fleet.models.enums import FailureClass, ModelTier, StructuredOutputMode
from fleet.models.tasks import BackendTarget, ModelCapabilities, Price, TokenUsage

if TYPE_CHECKING:  # avoid a real import-time cycle: orchestrator/retry.py imports FinishReason
    # from THIS module at its own module scope, so a top-level `from fleet.orchestrator.retry
    # import RetryPolicy` here would be circular. Resolved at runtime by `_default_retry_policy`'s
    # deferred import, executed only once `fleet.llm.client` has finished loading.
    from fleet.orchestrator.retry import RetryPolicy

FinishReason = Literal["stop", "length", "refusal", "tool_call", "filtered"]
"""Why generation stopped, as the transport reported it. Carried out of the backend because the
five are NOT interchangeable downstream: `length` is a truncated reply, and treating it as a schema
violation would fail the identical oversized call over three tiers to reproduce one truncation."""

FailoverTrigger = Literal["CONNECTION", "SERVER_ERROR", "RATE_LIMIT", "SCHEMA_UNSATISFIED"]
"""The §11.8 trigger set, exhaustively. `length` is deliberately absent: truncation is a property
of the request, not of the target, so it can never move a call to the next `BackendTarget`."""


# ---------------------------------------------------------------------------------------------
# Errors. Typed, never bools (Rule 11): a caller must be able to tell a truncated reply from an
# unsatisfiable schema from a spent budget WITHOUT parsing a message string, because those three
# have three different remedies (raise the cap / fail the target over / stop).
# ---------------------------------------------------------------------------------------------


class LlmError(Exception):
    """Base for every error crossing the §7.7 boundary."""


class UnknownRole(LlmError):
    """A role that `config/models.yaml` does not declare. A startup/config error, never a
    default to the expensive tier."""

    def __init__(self, role: str) -> None:
        super().__init__(f"undeclared LLM role: {role!r}")
        self.role = role


class UnknownBackend(LlmError):
    """A `BackendTarget.backend` naming a transport the registry does not hold — the §13 row 36
    startup error, surfaced here because that is where the target is first dereferenced."""

    def __init__(self, backend: str) -> None:
        super().__init__(f"no registered ModelBackend named {backend!r}")
        self.backend = backend


class UnpricedTarget(LlmError):
    """§9 rule 5 / §11.2: every target declares `price` or the literal `free`. An unpriced target
    is a config error, not a $0.00 call — a hosted target nobody priced would leave `spent_usd`
    at zero for a 250-repo run while the invoice arrived anyway."""

    def __init__(self, target: BackendTarget) -> None:
        super().__init__(f"target {target.backend}:{target.model_id} has no declared price")
        self.target = target


class BudgetExhausted(LlmError):
    """The caller's REMAINING ceiling would be broken by this dispatch. Raised BEFORE the backend
    is invoked, and again before each failover target, because a ceiling checked after the call is
    a ceiling that has already been broken (§11.2)."""

    failure_class: ClassVar[FailureClass] = FailureClass.BUDGET_EXHAUSTED

    def __init__(self, reason: str, *, target: BackendTarget | None = None) -> None:
        super().__init__(reason)
        self.target = target


class OutputTruncated(LlmError):
    """`finish_reason == "length"`. NOT a schema violation, NOT a failover trigger, spends no
    repair, and is excluded from `CapabilityDrift` accounting (§7.7, §11.8, §13 row 47). Retried
    on the SAME target with a raised `max_output_tokens`."""

    def __init__(self, target: BackendTarget, attempted_max_output_tokens: int) -> None:
        super().__init__(
            f"reply truncated at max_output_tokens={attempted_max_output_tokens} "
            f"on {target.backend}:{target.model_id}",
        )
        self.target = target
        self.attempted_max_output_tokens = attempted_max_output_tokens


class MalformedReply(LlmError):
    """The turn carried nothing this rung can parse — empty text, or `TOOL_CALL` with no
    arguments object. Repairable exactly like a `ValidationError`; loud, never a clean finish."""


class SchemaUnsatisfied(LlmError):
    """The rung plus `llm.max_schema_repairs` failed to produce a reply that validates. A TARGET
    failure, so it is a §11.8 failover trigger, not an immediate task failure."""

    def __init__(self, target: BackendTarget, repairs: int, detail: str) -> None:
        super().__init__(
            f"{target.backend}:{target.model_id} failed the response schema after "
            f"{repairs} repair(s): {detail}",
        )
        self.target = target
        self.repairs = repairs
        self.detail = detail


class ModelRefused(LlmError):
    """`finish_reason` of `refusal` or `filtered`. Like truncation this is a property of the
    request rather than of the endpoint, so it is surfaced typed instead of being retried around
    the tier."""

    def __init__(self, target: BackendTarget, finish_reason: FinishReason) -> None:
        super().__init__(f"{target.backend}:{target.model_id} returned {finish_reason}")
        self.target = target
        self.finish_reason = finish_reason


class TransportError(LlmError):
    """What a backend raises for a connection-level or 5xx failure that survived its own transient
    layer. The §11.8 failover triggers 1-3; backends raise it, the client acts on it."""

    def __init__(self, message: str, *, trigger: FailoverTrigger = "CONNECTION") -> None:
        super().__init__(message)
        self.trigger = trigger


class TierUnavailable(LlmError):
    """Every target for the tier is unhealthy, or `max_targets_per_call` was reached without a
    validated response. Fail closed (§11.8, exit 8); never a silent downgrade to another tier."""

    failure_class: ClassVar[FailureClass] = FailureClass.BACKEND_UNAVAILABLE

    def __init__(self, tier: ModelTier, targets_tried: Sequence[str]) -> None:
        super().__init__(f"tier {tier} exhausted after targets: {', '.join(targets_tried) or '-'}")
        self.tier = tier
        self.targets_tried = tuple(targets_tried)


# ---------------------------------------------------------------------------------------------
# Payload models (SPEC §7.7)
# ---------------------------------------------------------------------------------------------


class Message(BaseModel):
    """The neutral payload. Deliberately poorer than any vendor's message type: role + text.
    A backend that needs richer content blocks constructs them from this, never the reverse.
    `tool` exists because the TOOL_CALL rung is a two-turn protocol — the reply's arguments have to
    travel back in as a turn, and without this member they would be re-flattened into a user
    message, which is the shape the negotiator is trying to avoid recording as PROMPTED."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str


class CallBudget(BaseModel):
    """What is LEFT, not what was granted. Constructed by the runner from the §11.2 reserve-then-
    spend ledger, carried on `WorkerContext` (§7.1), and narrowed on every dispatch. Without it the
    `WorkerBudget.max_cost_usd` ceiling could only be observed after it was already overshot, and
    the reservation would have nothing to reserve against."""

    remaining_tokens: int = Field(ge=0)
    remaining_usd: float = Field(ge=0.0)
    deadline: float  # absolute `loop.time()`; the same clock as WorkerContext


class StreamEvent(BaseModel):
    """The ONLY thing streaming exposes. Deliberately NOT partial text: a caller handed tokens
    would parse them, and validation happens exactly once, on the complete reply."""

    output_tokens: int = 0  # cumulative, monotonic
    elapsed_ms: int = 0


class BackendReply(BaseModel):
    """One transport turn, structurally. `text` and `tool_arguments` are separate because the
    TOOL_CALL rung's answer is an arguments OBJECT: scraping it back out of a string is a parser
    the harness would then own, and a model emitting a tool call plus an assistant turn would
    defeat it outright."""

    text: str | None = None
    tool_arguments: dict[str, object] | None = None
    usage: TokenUsage
    finish_reason: FinishReason


class ModelResponse[T: BaseModel](BaseModel):
    """What every call returns. `value` is ALREADY validated — there is no unvalidated path out
    of this module, which is what makes the backends interchangeable (ADR-0002)."""

    value: T
    usage: TokenUsage
    mode: StructuredOutputMode  # which negotiation rung actually produced `value`
    finish_reason: FinishReason  # surfaced, not swallowed; `length` never reaches Pydantic
    repairs: int = 0  # parse-and-repair re-asks spent; > 0 is a §13 row 37 signal


class CapabilityDrift(BaseModel):
    """A response produced at a LOWER rung than the profile promised (§13 row 37). Emitted whether
    or not the call succeeded, so a local server silently dropping guided JSON shows up as a
    finding rather than as a slow rise in repair counts. Never emitted for truncation."""

    role: str
    tier: ModelTier
    backend: str
    model_id: str
    promised: StructuredOutputMode
    actual: StructuredOutputMode


class BackendFailover(BaseModel):
    """One `backend_failover` event (§11.8): both targets and the trigger. A failover is never an
    attempt — it increments `attempts.llm_failovers`, not `phases.attempts`."""

    role: str
    tier: ModelTier
    from_backend: str
    from_model_id: str
    to_backend: str
    to_model_id: str
    trigger: FailoverTrigger


class LlmCall(BaseModel):
    """One `llm_call` event (§12.18): the fields SPEC §12.18 lists, verbatim — `role`, `tier`,
    `backend`, the resolved `model_id`, `structured_output_mode`, token counts, `cost_usd`, and
    `latency_ms`.

    Emitted once per *actual provider call* — one `backend.invoke()` that RETURNS, whatever its
    outcome — not once per `complete()` (which may walk several targets) and not once per
    `_call_target()` (which may retry the SAME target for a truncation or a schema repair, each
    retry its own billed call). ADR-0012 says "token usage, cost, and latency are logged as
    `llm_call` events so per-repo spend is a `jq` away"; a repair re-ask spends real tokens on the
    same target, so folding it into one event per `_call_target()` would undercount exactly the
    spend an operator is `jq`-ing for.

    A raised `TransportError` is NOT covered — there is no `BackendReply` to report `input_tokens`
    /`output_tokens`/`cost_usd` from, and that failure is `_emit_failover`'s job, not this one's.

    `level` is `"error"` for a reply the transport itself flags as unusable (`finish_reason` of
    `refusal` or `filtered`, §12.18's neighbouring "recoverable error" sink) and `"info"`
    otherwise — including a truncated reply, which is ordinary ladder behaviour handled by
    `_raise_cap` on the SAME target, not an error.
    """

    role: str
    tier: ModelTier
    backend: str
    model_id: str
    structured_output_mode: StructuredOutputMode
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    level: Literal["info", "error"]
    base_url: str | None = None
    """ADR-0149/§12.51(i): the ENDPOINT that answered — the resolved replica for a `base_urls`
    target, the target's own `base_url` otherwise. This is where the per-endpoint split of a run
    is recorded; `backend`/`model_id` are identical across replicas and cannot show it."""


# ---------------------------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------------------------


class ModelClient(Protocol):
    """The ONLY way the harness talks to a model. ~70 lines of interface, zero dependencies
    beyond Pydantic. Explicitly NOT LangChain, LangGraph, DeepAgents, or LiteLLM: we need one
    method, not an agent framework."""

    async def complete[T: BaseModel](
        self,
        role: str,
        messages: Sequence[Message],
        response_model: type[T],
        *,
        tier_override: ModelTier | None = None,
        max_output_tokens: int | None = None,
        timeout_s: float | None = None,
        budget: CallBudget | None = None,
    ) -> ModelResponse[T]:
        """Route `role` → tier → ordered `BackendTarget`s, negotiate structured output, call,
        validate, and return. Raises `SchemaUnsatisfied` after the repair budget, and
        `TierUnavailable` when every target for the tier is unhealthy (§11.8).

        Raises `BudgetExhausted` BEFORE dispatch when the projected cost of the call exceeds
        `budget.remaining_usd`/`remaining_tokens`, or when `budget.deadline` has passed — the check
        is inside the call path because that is the only place the target's price is known, and a
        ceiling checked after the call is a ceiling that has already been broken. Failover to a
        further `BackendTarget` re-checks against the same budget, since the next target may be
        dearer than the one that just failed."""
        ...

    def stream[T: BaseModel](
        self,
        role: str,
        messages: Sequence[Message],
        response_model: type[T],
        *,
        budget: CallBudget | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Progress only. Its ONLY contract is token-count events the runner forwards as worker
        heartbeats, so a legitimate 900 s HEAVY call is not reaped as a stale lease (§11.5).
        Declared `def` returning `AsyncIterator`, not `async def`, because an async generator's
        type is `Callable[..., AsyncIterator[...]]` and the coroutine form would not match one
        under `mypy --strict`."""
        ...

    async def capabilities(self, role: str) -> ModelCapabilities:
        """Declared (not probed) capabilities of the target `role` would currently route to."""
        ...


@runtime_checkable
class ModelBackend(Protocol):
    """One transport. A new provider is ONE file under `llm/backends/` + one @register_backend."""

    name: ClassVar[str]  # registry key; matches `BackendTarget.backend`
    version: ClassVar[int]  # bump invalidates nothing — the cache keys on `model_id`

    def declared_capabilities(self, target: BackendTarget) -> ModelCapabilities: ...

    async def invoke(
        self,
        target: BackendTarget,
        messages: Sequence[Message],
        schema: dict[str, object] | None,
        mode: StructuredOutputMode,
        *,
        max_output_tokens: int,
        timeout_s: float,
    ) -> BackendReply:
        """Return the raw turn — text or tool arguments — plus usage AND `finish_reason`. A backend
        NEVER validates, never retries a schema failure, and never picks its own mode; those belong
        to the client, so every backend behaves identically at the boundary that matters. It also
        never DECIDES anything from `finish_reason`: it reports what the transport said and the
        client acts on it."""
        ...


@runtime_checkable
class RepoScopedModelClient(Protocol):
    """ADR-0149: a `ModelClient` that can bind the `repo_id` its calls are made for, so replica
    selection (§12.51) keys on the repo WITHOUT `complete()` growing a parameter every backend and
    fake would have to care about — the same reasoning, one layer down, as `llm/cache.py`'s
    `ScopedModelClient`. Named `for_repo`, not `scoped`: `ScopedModelClient` is
    `@runtime_checkable`, which checks method NAMES only, so a `LadderModelClient.scoped(repo_id)`
    would satisfy `isinstance(..., ScopedModelClient)` and `rewrite.py::_scoped_client` would call
    it with `context_policy=` and raise `TypeError` wherever a bare ladder client is `ctx.llm`."""

    def for_repo(self, repo_id: str) -> ModelClient: ...


def scope_to_repo(client: ModelClient, repo_id: str) -> ModelClient:
    """`client.for_repo(repo_id)` when the client supports it, `client` unchanged otherwise — a
    bare test fake need not grow a method it has no use for (the `ScopedModelClient` rule)."""
    if isinstance(client, RepoScopedModelClient):
        return client.for_repo(repo_id)
    return client


def replica_index(repo_id: str, replicas: int) -> int:
    """§12.51(ii): a STABLE map from `repo_id` to one of `replicas` endpoints. sha256, never the
    builtin `hash()` — that is salted per process (`PYTHONHASHSEED`), so a resumed run or a second
    worker process would send the same repo to a different replica."""
    if replicas < 1:
        raise ValueError(f"replica_index needs at least one replica, got {replicas}")
    return int.from_bytes(hashlib.sha256(repo_id.encode("utf-8")).digest()[:8], "big") % replicas


def resolve_replicas(target: BackendTarget, repo_id: str | None) -> tuple[BackendTarget, ...]:
    """ADR-0149: the concrete per-call targets one logical `target` expands to, in call order.

    A target with no `base_urls` is returned as-is. Otherwise one `model_copy` per replica with
    `base_url` set and `base_urls` cleared — the registered target is never mutated — rotated so
    the repo's affine replica (`replica_index`) comes first and its peers follow as the in-target
    failover order. An unbound client (`repo_id is None`) starts at replica 0."""
    urls = target.base_urls
    if not urls:
        return (target,)
    start = 0 if repo_id is None else replica_index(repo_id, len(urls))
    ordered = (*urls[start:], *urls[:start])
    return tuple(target.model_copy(update={"base_url": u, "base_urls": None}) for u in ordered)


class TierRoute(BaseModel):
    """What a role resolved to: the tier, and the tier's ordered target list."""

    tier: ModelTier
    targets: tuple[BackendTarget, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _one_effort_per_backend_and_model_id(self) -> TierRoute:
        """`(backend, model_id)` must name ONE `effort` within a route, because that pair is the
        whole of the identity a call carries back.

        `llm/cache.py`'s `_target_for` recovers the target that ANSWERED — and with it that
        target's `effort`, a cache-KEY component — by matching `(usage.backend, usage.model_id)`
        against these targets. `TokenUsage` has no `effort` field, so two targets sharing that
        pair and declaring different `effort` are indistinguishable to it: whichever iterates
        first wins regardless of which one answered. A standby's answer is then stored under the
        primary's key, and the next call with a healthy primary HITS and is served it — the exact
        §13 row 39 poisoning `_store_response` exists to prevent, silent because the served row
        looks like a legitimate hit. Nothing in the loader rejected such a profile, so a `degrade
        the same model on failover` config (`effort: high` then `effort: low`) reproduced it.

        Declaring the same `(backend, model_id)` TWICE stays legal when the two agree on
        `effort` — the same model behind two `base_url`s is a legitimate failover pair, and the
        recovered `effort` is correct whichever of them answered. It is the disagreement, and
        only the disagreement, that is unrecoverable. Making it representable again needs the
        answering `effort` carried back on the call, which is a `TokenUsage` field and a write
        path this validator deliberately does not invent.
        """
        seen: dict[tuple[str, str], str | None] = {}
        for target in self.targets:
            identity = (target.backend, target.model_id)
            if identity in seen and seen[identity] != target.effort:
                raise ValueError(
                    f"tier {self.tier.value} routes {target.backend}:{target.model_id} twice "
                    f"with different effort ({seen[identity]!r} then {target.effort!r}): the "
                    "answering target would not be recoverable, so the LLM cache would store "
                    "one target's answer under the other's key"
                )
            seen[identity] = target.effort
        return self


class RoleRouter(Protocol):
    """`routing.py`'s surface, declared here so the client depends on the interface and not on the
    YAML loader. Rejects unknown roles (`UnknownRole`), unknown backends, and empty tiers."""

    def resolve(self, role: str, *, tier_override: ModelTier | None = None) -> TierRoute: ...


# ---------------------------------------------------------------------------------------------
# Registry (§7.2 rules, fifth user)
# ---------------------------------------------------------------------------------------------

_BACKENDS: dict[str, ModelBackend] = {}


def register_backend[B: type[ModelBackend]](cls: B) -> B:
    """Same decorator, same duplicate-is-a-startup-error rule as §7.2/§7.3/§7.5/§7.6."""
    if cls.name in _BACKENDS:
        raise RuntimeError(f"duplicate ModelBackend: {cls.name}")
    _BACKENDS[cls.name] = cls()
    return cls


def registry() -> dict[str, ModelBackend]:
    """A snapshot of what is registered. Callers inject this; nothing reaches for a global."""
    return dict(_BACKENDS)


def discover() -> dict[str, ModelBackend]:
    """pkgutil walk of `fleet.llm.backends`. Unlike EcosystemAdapter's, this registry is NOT total
    over an enum — backends are open-ended. It IS total over the active profile: every `backend`
    named in config/models.yaml must resolve, checked at RunContext construction (§13 row 36).

    A backend whose SDK is not installed fails its import here and is simply not registered, which
    is not an error: a run that does not name it does not need it."""
    try:
        package = import_module("fleet.llm.backends")
    except ImportError:
        return registry()
    for module in pkgutil.iter_modules(list(getattr(package, "__path__", []))):
        try:
            import_module(f"fleet.llm.backends.{module.name}")
        except ImportError:
            continue
    return registry()


# ---------------------------------------------------------------------------------------------
# Pricing and the negotiation ladder — pure functions, so `negotiate.py` can adopt them unchanged
# ---------------------------------------------------------------------------------------------

_LADDER: tuple[StructuredOutputMode, ...] = (
    StructuredOutputMode.JSON_SCHEMA,
    StructuredOutputMode.TOOL_CALL,
    StructuredOutputMode.CONSTRAINED,
    StructuredOutputMode.PROMPTED,
)
_RANK: dict[StructuredOutputMode, int] = {mode: i for i, mode in enumerate(_LADDER)}


def estimate_cost_usd(target: BackendTarget, input_tokens: int, output_tokens: int) -> float:
    """`price(target, n)` of §11.2, exactly. `free` reserves and spends 0.0; an *absent* price is
    `UnpricedTarget`, never 0.0 (§9 rule 5)."""
    price: object = target.price
    if isinstance(price, Price):
        return (price.in_per_mtok * input_tokens + price.out_per_mtok * output_tokens) / 1e6
    if price == "free":
        return 0.0
    raise UnpricedTarget(target)


def negotiate(caps: ModelCapabilities) -> StructuredOutputMode:
    """Walk the fixed ladder best-first and take the highest rung `caps` can honour. PROMPTED is
    the floor, always available — a small local model with no structured-output support is a
    legitimate CHEAP target and refusing to talk to it would defeat the point."""
    if caps.supports_json_schema:
        return StructuredOutputMode.JSON_SCHEMA
    if caps.supports_tools:
        return StructuredOutputMode.TOOL_CALL
    if caps.supports_constrained_decoding:
        return StructuredOutputMode.CONSTRAINED
    return StructuredOutputMode.PROMPTED


def promised_mode(caps: ModelCapabilities) -> StructuredOutputMode:
    """The best rung `structured_output_modes` PROMISES. The booleans say what we may attempt;
    this says what the profile claimed — the gap between the two is `CapabilityDrift`, which gives
    the two overlapping declarations in `ModelCapabilities` one job each."""
    return min(caps.structured_output_modes, key=lambda m: _RANK[m], default=_LADDER[-1])


def merge_capabilities(declared: ModelCapabilities, target: BackendTarget) -> ModelCapabilities:
    """declared ⊕ `capabilities_override` (§7.7 `capabilities.py`). An unknown override key is a
    `ValidationError` — a config typo must be loud, not silently ignored."""
    if not target.capabilities_override:
        return declared
    return ModelCapabilities.model_validate(
        {**declared.model_dump(), **target.capabilities_override},
    )


def estimate_input_tokens(messages: Sequence[Message]) -> int:
    """A deterministic pre-dispatch estimate (~4 chars/token + per-message overhead). Deliberately
    arithmetic, not a tokenizer call: the budget gate must not itself require the vendor SDK."""
    return sum(len(m.content) for m in messages) // 4 + 8 * len(messages)


# ---------------------------------------------------------------------------------------------
# Reference client
# ---------------------------------------------------------------------------------------------

_PROMPTED_INSTRUCTION = (
    "Respond with a single JSON object and nothing else — no prose, no markdown fence. "
    "It must validate against this JSON Schema:\n{schema}"
)
_REPAIR_INSTRUCTION = (
    "That reply did not validate. Return the corrected JSON object only. "
    "The validator said, verbatim:\n{error}"
)


class CallPolicy(BaseModel):
    """The §9 `llm:` knobs this module actually reads. Injected, so a test does not have to load
    config and a run does not have to hard-code a default."""

    max_schema_repairs: int = Field(default=1, ge=0)
    max_targets_per_call: int = Field(default=3, ge=1)
    max_truncation_retries: int = Field(default=2, ge=0)
    truncation_growth: float = Field(default=2.0, gt=1.0)
    default_max_output_tokens: int = Field(default=4096, gt=0)
    default_timeout_s: float = Field(default=120.0, gt=0.0)
    heartbeat_interval_s: float = Field(default=5.0, ge=0.0)
    open_after_failures: int = Field(default=3, ge=1)
    """§11.8's `BackendHealth` breaker: consecutive qualifying failures (never a single 429 —
    see `llm/failover.py`'s module docstring) before a target is marked `DOWN`. Mirrors
    `llm.failover.open_after_failures`'s own default."""
    cooldown_s: float = Field(default=120.0, ge=0.0)
    """§11.8: seconds a `DOWN` target is skipped before its one `HALF_OPEN` probe. Mirrors
    `llm.failover.cooldown_s`'s own default."""


def _default_retry_policy() -> RetryPolicy:
    """Deferred import — see the `TYPE_CHECKING` block's comment for why a top-level one would
    cycle. `RetryPolicy()`'s own defaults are §11.8's: `DEFAULT_MAX_TRANSIENT_RETRIES`,
    `DEFAULT_BACKOFF_BASE_S`, `DEFAULT_BACKOFF_CAP_S` — reused, not reinvented (ADR-0132)."""
    from fleet.orchestrator.retry import RetryPolicy

    return RetryPolicy()


class LadderModelClient:
    """The reference `ModelClient`. Everything it talks to is injected: a `RoleRouter`, a mapping
    of registered `ModelBackend`s, four event sinks, a clock and a retry policy. There is no
    vendor import here and no code path that reaches for one."""

    def __init__(
        self,
        router: RoleRouter,
        backends: Mapping[str, ModelBackend] | None = None,
        *,
        policy: CallPolicy | None = None,
        on_drift: Callable[[CapabilityDrift], None] | None = None,
        on_failover: Callable[[BackendFailover], None] | None = None,
        on_llm_call: Callable[[LlmCall], None] | None = None,
        on_health_transition: Callable[[BackendHealthTransition], None] | None = None,
        retry_policy: RetryPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._router = router
        self._backends: Mapping[str, ModelBackend] = registry() if backends is None else backends
        self._policy = policy or CallPolicy()
        self._on_drift = on_drift
        self._on_failover = on_failover
        self._on_llm_call = on_llm_call
        self._clock = clock
        self._retry_policy = retry_policy or _default_retry_policy()
        # §11.8's breaker: one per client, matching its own "in-memory, per-run" contract — a
        # fresh `LadderModelClient` (what a resumed run constructs) starts every target at UP.
        self._health = BackendHealth(
            open_after_failures=self._policy.open_after_failures,
            cooldown_s=self._policy.cooldown_s,
            clock=clock,
            on_transition=on_health_transition,
        )
        self._repo_id: str | None = None

    def for_repo(self, repo_id: str) -> LadderModelClient:
        """ADR-0149: a view of this client whose calls are made for `repo_id`, which picks the
        replica of every `base_urls` target (§12.51). A shallow copy on purpose: the router,
        backends, sinks and — load-bearing — the `BackendHealth` breaker are SHARED, so a replica
        one repo's call found dead is dead for every repo, exactly as for an ordinary target."""
        view = copy.copy(self)
        view._repo_id = repo_id
        return view

    # -- public surface ------------------------------------------------------------------------

    async def complete[T: BaseModel](
        self,
        role: str,
        messages: Sequence[Message],
        response_model: type[T],
        *,
        tier_override: ModelTier | None = None,
        max_output_tokens: int | None = None,
        timeout_s: float | None = None,
        budget: CallBudget | None = None,
    ) -> ModelResponse[T]:
        """See `ModelClient.complete`."""
        route = self._router.resolve(role, tier_override=tier_override)
        schema: dict[str, object] = dict(response_model.model_json_schema())
        # `max_targets_per_call` bounds LOGICAL targets (entries of the tier's list, which is what
        # the knob has always counted); each then expands to its replicas, the repo's affine one
        # first (ADR-0149). A replica hop is an ordinary hop of this same loop — health-gated,
        # `backend_failover`-emitted, and counted in `llm_failovers` — never a separate mechanism.
        targets = [
            concrete
            for logical in route.targets[: self._policy.max_targets_per_call]
            for concrete in resolve_replicas(logical, self._repo_id)
        ]
        timeout = self._policy.default_timeout_s if timeout_s is None else timeout_s
        requested = (
            self._policy.default_max_output_tokens
            if max_output_tokens is None
            else max_output_tokens
        )

        tried: list[str] = []
        last: LlmError | None = None
        for index, target in enumerate(targets):
            tried.append(f"{target.backend}:{target.model_id}")
            if not self._health.may_call(target, route.tier):
                # DOWN and not yet eligible for its one HALF_OPEN probe (§11.8, ADR-0132): skip
                # straight to the next target, exactly like an immediate failover — no call, no
                # budget check, no drift check against an endpoint we are not going to use.
                continue
            try:
                backend = self._backend_for(target)
                caps = merge_capabilities(backend.declared_capabilities(target), target)

                # Re-checked HERE, per target: the next target may be dearer than the one that
                # just failed, so a budget cleared once is not a budget cleared for the ladder
                # (§11.2).
                cap = min(requested, caps.max_output_tokens)
                self._check_budget(budget, target, estimate_input_tokens(messages), cap)

                mode = negotiate(caps)
                self._emit_drift(role, route.tier, target, caps, mode)
                response = await self._call_target(
                    role=role,
                    tier=route.tier,
                    target=target,
                    backend=backend,
                    caps=caps,
                    messages=messages,
                    response_model=response_model,
                    schema=schema,
                    mode=mode,
                    max_output_tokens=cap,
                    timeout_s=timeout,
                    budget=budget,
                )
            except (SchemaUnsatisfied, TransportError) as exc:
                # The ONLY two failover paths. `OutputTruncated`, `BudgetExhausted` and
                # `ModelRefused` deliberately propagate: none of them says anything about whether
                # this endpoint is worth using.
                last = exc
                trigger: FailoverTrigger = (
                    "SCHEMA_UNSATISFIED" if isinstance(exc, SchemaUnsatisfied) else exc.trigger
                )
                if isinstance(exc, TransportError):
                    # A QUALIFYING failure (§11.8, ADR-0132): by the time a TransportError
                    # reaches here, `_call_target`'s own backoff arm has already absorbed every
                    # RATE_LIMIT it could and CONNECTION/SERVER_ERROR never had one to absorb —
                    # this is never fired for a single 429 mid-backoff. SchemaUnsatisfied never
                    # counts: it is a negotiation-ladder problem (§7.7), not evidence the
                    # endpoint is unreachable — but a HALF_OPEN probe still needs resolving, so
                    # it goes through `abandon_probe` instead (a no-op for a non-probing target).
                    self._health.record_failure(target, route.tier)
                else:
                    self._health.abandon_probe(target, route.tier)
                if index + 1 < len(targets):
                    self._emit_failover(role, route.tier, target, targets[index + 1], trigger)
                continue
            except BaseException:
                # Review fix (round 1): every OTHER exit door — `OutputTruncated`, `ModelRefused`,
                # `BudgetExhausted`, `UnknownBackend`, a cancellation, anything `_check_budget` or
                # `_backend_for` raises — used to leave a `HALF_OPEN` probe wedged there FOREVER,
                # because the only two resolutions were `record_success`/`record_failure` above
                # and neither fires for these. Worse than not building the breaker at all: one
                # unlucky probe silently kills a target for every worker sharing this client for
                # the rest of the run. `abandon_probe` is a no-op for a non-probing (UP) target,
                # so this is safe to call unconditionally before letting the exception propagate
                # exactly as it always did — this changes no exception's type or message, only
                # adds the missing resolution.
                self._health.abandon_probe(target, route.tier)
                raise
            self._health.record_success(target, route.tier)
            # ADR-0107, §11.8: `index` at the point `_call_target` succeeded IS the failover-hop
            # count for this call — 0 for the first target, 1 for one hop, etc. Stamped only when
            # non-zero so the common (zero-hop) case allocates nothing extra.
            if index > 0:
                response = response.model_copy(
                    update={"usage": response.usage.model_copy(update={"llm_failovers": index})}
                )
            return response
        raise TierUnavailable(route.tier, tried) from last

    def stream[T: BaseModel](
        self,
        role: str,
        messages: Sequence[Message],
        response_model: type[T],
        *,
        budget: CallBudget | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """See `ModelClient.stream`. `def` returning an `AsyncIterator`, not `async def`."""
        return self._heartbeats(role, messages, response_model, budget=budget)

    async def capabilities(self, role: str) -> ModelCapabilities:
        """Declared, never probed — a run's plan must not depend on a network call."""
        route = self._router.resolve(role)
        target = route.targets[0]
        backend = self._backend_for(target)
        return merge_capabilities(backend.declared_capabilities(target), target)

    # -- internals -----------------------------------------------------------------------------

    async def _heartbeats[T: BaseModel](
        self,
        role: str,
        messages: Sequence[Message],
        response_model: type[T],
        *,
        budget: CallBudget | None,
    ) -> AsyncIterator[StreamEvent]:
        """Token-count progress for the liveness reaper, and nothing else. The first event is
        emitted BEFORE the call can possibly have finished — a 900 s HEAVY call that emitted its
        first heartbeat only on completion is exactly the stale-lease kill this exists to prevent
        (§11.5). Counts are cumulative and monotonic; partial text is never exposed."""
        started = self._clock()
        task: asyncio.Task[ModelResponse[T]] = asyncio.ensure_future(
            self.complete(role, messages, response_model, budget=budget),
        )
        try:
            yield StreamEvent(output_tokens=0, elapsed_ms=self._elapsed_ms(started))
            while not task.done():
                await asyncio.wait({task}, timeout=self._policy.heartbeat_interval_s)
                yield StreamEvent(output_tokens=0, elapsed_ms=self._elapsed_ms(started))
            response = task.result()  # a failed call raises out of the stream, never silently ends
            yield StreamEvent(
                output_tokens=response.usage.output_tokens,
                elapsed_ms=self._elapsed_ms(started),
            )
        finally:
            if not task.done():
                task.cancel()

    def _elapsed_ms(self, started: float) -> int:
        return max(int((self._clock() - started) * 1000), 0)

    def _backend_for(self, target: BackendTarget) -> ModelBackend:
        backend = self._backends.get(target.backend)
        if backend is None:
            raise UnknownBackend(target.backend)
        return backend

    def _check_budget(
        self,
        budget: CallBudget | None,
        target: BackendTarget,
        input_tokens: int,
        max_output_tokens: int,
    ) -> None:
        """BEFORE dispatch, and again per failover target. `estimate_cost_usd` is what makes this
        the only place the check can live: the price belongs to the target, not to the caller."""
        if budget is None:
            return
        if self._clock() >= budget.deadline:
            raise BudgetExhausted("call deadline passed before dispatch", target=target)
        projected_tokens = input_tokens + max_output_tokens
        if projected_tokens > budget.remaining_tokens:
            raise BudgetExhausted(
                f"projected {projected_tokens} tokens exceeds remaining "
                f"{budget.remaining_tokens}",
                target=target,
            )
        projected_usd = estimate_cost_usd(target, input_tokens, max_output_tokens)
        if projected_usd > budget.remaining_usd:
            raise BudgetExhausted(
                f"projected ${projected_usd:.6f} exceeds remaining ${budget.remaining_usd:.6f} "
                f"on {target.backend}:{target.model_id}",
                target=target,
            )

    def _emit_drift(
        self,
        role: str,
        tier: ModelTier,
        target: BackendTarget,
        caps: ModelCapabilities,
        actual: StructuredOutputMode,
    ) -> None:
        """Emitted once per TARGET, not per retry: a truncation retry must not manufacture a
        second drift finding against a healthy endpoint."""
        promised = promised_mode(caps)
        if self._on_drift is None or _RANK[actual] <= _RANK[promised]:
            return
        self._on_drift(
            CapabilityDrift(
                role=role,
                tier=tier,
                backend=target.backend,
                model_id=target.model_id,
                promised=promised,
                actual=actual,
            ),
        )

    def _emit_failover(
        self,
        role: str,
        tier: ModelTier,
        source: BackendTarget,
        destination: BackendTarget,
        trigger: FailoverTrigger,
    ) -> None:
        if self._on_failover is None:
            return
        self._on_failover(
            BackendFailover(
                role=role,
                tier=tier,
                from_backend=source.backend,
                from_model_id=source.model_id,
                to_backend=destination.backend,
                to_model_id=destination.model_id,
                trigger=trigger,
            ),
        )

    def _emit_llm_call(
        self,
        role: str,
        tier: ModelTier,
        target: BackendTarget,
        mode: StructuredOutputMode,
        reply: BackendReply,
        started: float,
    ) -> None:
        """One §12.18 `llm_call` event per completed `backend.invoke()`. See `LlmCall`'s
        docstring for why this fires once per raw call rather than once per `_call_target()`."""
        if self._on_llm_call is None:
            return
        self._on_llm_call(
            LlmCall(
                role=role,
                tier=tier,
                backend=target.backend,
                # Same convention as `_stamp`: `usage.model_id` is the CONFIGURED id, never the
                # transport's served/resolved name (client.py:896-909).
                model_id=reply.usage.model_id or target.model_id,
                structured_output_mode=mode,
                input_tokens=reply.usage.input_tokens,
                output_tokens=reply.usage.output_tokens,
                cost_usd=estimate_cost_usd(
                    target, reply.usage.input_tokens, reply.usage.output_tokens
                ),
                latency_ms=self._elapsed_ms(started),
                level="error" if reply.finish_reason in ("refusal", "filtered") else "info",
                base_url=target.base_url,
            ),
        )

    async def _call_target[T: BaseModel](
        self,
        *,
        role: str,
        tier: ModelTier,
        target: BackendTarget,
        backend: ModelBackend,
        caps: ModelCapabilities,
        messages: Sequence[Message],
        response_model: type[T],
        schema: dict[str, object],
        mode: StructuredOutputMode,
        max_output_tokens: int,
        timeout_s: float,
        budget: CallBudget | None,
    ) -> ModelResponse[T]:
        """One target's whole life: same-target `RATE_LIMIT` backoff, truncation raises, budgeted
        repairs, and validation. The order of the length/refusal guards is the point — `length` is
        checked FIRST, before the reply is ever handed to Pydantic (§13 row 47)."""
        base = _prepare_messages(messages, caps, schema, mode)
        conversation = list(base)
        repairs = 0
        truncations = 0
        rate_limit_retries = 0
        cap = max_output_tokens
        input_tokens = estimate_input_tokens(messages)

        while True:
            call_started = self._clock()
            try:
                reply = await backend.invoke(
                    target,
                    conversation,
                    schema if mode is not StructuredOutputMode.PROMPTED else None,
                    mode,
                    max_output_tokens=cap,
                    timeout_s=timeout_s,
                )
            except TransportError as exc:
                # §11.8/ADR-0132: the SAME-target backoff-retry arm, for RATE_LIMIT only —
                # CONNECTION and SERVER_ERROR have nothing to back off for (a dead socket does
                # not answer sooner for waiting, and a 5xx has already spent the SDK's own
                # `max_retries` budget per backend), so both propagate immediately exactly as
                # before. Reusing `orchestrator/retry.py`'s `RetryPolicy.backoff_delay` and
                # `max_transient_retries` rather than inventing new jitter math (ADR-0132
                # judgment call 1). Exhausting this bound is the "entire §11.8 backoff schedule"
                # §12.43 case (ii) names — a QUALIFYING failure `complete()` reports to
                # `BackendHealth`, never fired for a single absorbed 429.
                if exc.trigger != "RATE_LIMIT":
                    raise
                if rate_limit_retries >= self._retry_policy.max_transient_retries:
                    raise
                rate_limit_retries += 1
                await asyncio.sleep(self._retry_policy.backoff_delay(rate_limit_retries))
                continue  # SAME target, SAME conversation. No repair spent, no failover (yet).
            # "Measure around the actual provider call" (§12.18): THIS invocation, not the
            # ladder walk `complete()` may still be doing and not the retry loop this method is
            # in the middle of — each iteration here is its own billed request.
            self._emit_llm_call(role, tier, target, mode, reply, call_started)

            if reply.finish_reason == "length":
                truncations += 1
                cap = self._raise_cap(
                    target=target,
                    caps=caps,
                    budget=budget,
                    input_tokens=input_tokens,
                    current=cap,
                    truncations=truncations,
                )
                continue  # SAME target. No repair spent, no failover, no CapabilityDrift.

            if reply.finish_reason in ("refusal", "filtered"):
                raise ModelRefused(target, reply.finish_reason)

            try:
                value = _validate(reply, response_model, mode)
            except (ValidationError, ValueError, MalformedReply) as exc:
                detail = str(exc)
                if repairs >= self._policy.max_schema_repairs:
                    raise SchemaUnsatisfied(target, repairs, detail) from exc
                repairs += 1
                conversation = [*base, *_repair_turns(reply, mode, detail)]
                continue

            return ModelResponse(
                value=value,
                usage=_stamp(reply.usage, role, tier, target),
                mode=mode,
                finish_reason=reply.finish_reason,
                repairs=repairs,
            )

    def _raise_cap(
        self,
        *,
        target: BackendTarget,
        caps: ModelCapabilities,
        budget: CallBudget | None,
        input_tokens: int,
        current: int,
        truncations: int,
    ) -> int:
        """Raise `max_output_tokens` for the SAME target, bounded by the target's declared maximum
        and by the caller's `CallBudget`. Exhausting the raise is `FailureClass.BUDGET_EXHAUSTED`
        — an oversized request, not a backend fault — so it must never be a failover trigger."""
        truncated = OutputTruncated(target, current)
        if truncations > self._policy.max_truncation_retries:
            raise BudgetExhausted(
                f"{truncated} and the truncation retry budget "
                f"({self._policy.max_truncation_retries}) is spent",
                target=target,
            ) from truncated
        want = min(int(current * self._policy.truncation_growth), caps.max_output_tokens)
        if budget is not None:
            want = min(want, max(budget.remaining_tokens - input_tokens, 0))
        if want <= current:
            raise BudgetExhausted(
                f"{truncated} and max_output_tokens cannot be raised above {current} "
                f"(declared max {caps.max_output_tokens})",
                target=target,
            ) from truncated
        return want


def _protocol_conformance(client: LadderModelClient) -> ModelClient:
    """Compile-time only: `mypy --strict` fails here if the reference client ever drifts from the
    protocol the rest of the harness codes against."""
    return client


# ---------------------------------------------------------------------------------------------
# Rendering / parsing helpers
# ---------------------------------------------------------------------------------------------


def _prepare_messages(
    messages: Sequence[Message],
    caps: ModelCapabilities,
    schema: dict[str, object],
    mode: StructuredOutputMode,
) -> list[Message]:
    """`supports_system_prompt: false` folds the system turns into the first user message — the one
    transformation the negotiator may perform on content (§7.7). Under PROMPTED the schema is
    rendered into the prompt, because there is nowhere else to put it."""
    prepared = list(messages)
    if not caps.supports_system_prompt:
        systems = [m.content for m in prepared if m.role == "system"]
        rest = [m for m in prepared if m.role != "system"]
        if systems:
            folded = "\n\n".join(systems)
            if rest and rest[0].role == "user":
                rest[0] = Message(role="user", content=f"{folded}\n\n{rest[0].content}")
            else:
                rest.insert(0, Message(role="user", content=folded))
        prepared = rest
    if mode is StructuredOutputMode.PROMPTED:
        rendered = json.dumps(schema, sort_keys=True)
        prepared.append(
            Message(role="user", content=_PROMPTED_INSTRUCTION.format(schema=rendered)),
        )
    return prepared


def _repair_turns(reply: BackendReply, mode: StructuredOutputMode, detail: str) -> list[Message]:
    """The re-ask carries the validator's words VERBATIM and the offending turn, nothing else —
    no transcript history, no prior failed diffs (CLAUDE.md guardrail 5). Under TOOL_CALL the
    offending turn travels back as a `tool` message, which is why that role member exists."""
    if mode is StructuredOutputMode.TOOL_CALL:
        offending = Message(
            role="tool",
            content=json.dumps(reply.tool_arguments or {}, sort_keys=True),
        )
    else:
        offending = Message(role="assistant", content=reply.text or "")
    return [offending, Message(role="user", content=_REPAIR_INSTRUCTION.format(error=detail))]


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
    return body.rsplit("```", 1)[0].strip()


def _validate[T: BaseModel](
    reply: BackendReply,
    response_model: type[T],
    mode: StructuredOutputMode,
) -> T:
    """Validation is ALWAYS Pydantic on our side, at every rung including JSON_SCHEMA: a backend's
    enforcement is a hit-rate hint, never a guarantee. This is the invariant that makes the
    backends interchangeable (ADR-0002, ADR-0023)."""
    if mode is StructuredOutputMode.TOOL_CALL:
        if reply.tool_arguments is None:
            raise MalformedReply("TOOL_CALL rung returned no tool_arguments object")
        return response_model.model_validate(reply.tool_arguments)
    text = _strip_fence(reply.text or "")
    if not text:
        raise MalformedReply("empty reply text")
    return response_model.model_validate(json.loads(text))


def _stamp(usage: TokenUsage, role: str, tier: ModelTier, target: BackendTarget) -> TokenUsage:
    """Attribute the usage to the target that actually answered, and price it — §11.2 writes this
    row in four places, so it must not be the caller's job to reconstruct who was called.

    **`model_id` is NOT part of "what the backend reported".** It is the CONFIGURED id from
    config/models.yaml, and an adapter MUST leave `usage.model_id` either empty or equal to
    `target.model_id` verbatim. It is a cache-KEY component whose two sides are built from
    different objects: the READ key from the config string (`llm/cache.py` `_key_parts`) and the
    WRITE key from `usage.model_id` (`_store_response`). An adapter that fills it from the API
    response's served/resolved name — a snapshot id like `...-20250219` — makes the two disagree
    on EVERY call: a permanent, silent 100% cache miss that is indistinguishable from a cold
    cache, because `attempts.llm_cache_hit` simply stays 0. (`_target_for` also matches on
    (backend, model_id), so it stops finding the answering target and `effort` degrades to the
    primary's.) Three adapters made exactly this mistake in one round.

    The `or` below is a DEFAULT for adapters that leave the field empty, not an invitation to
    supply something else. Surfacing the served id is a legitimate want — it needs a SEPARATE
    field or a log line, never this one.
    """
    return usage.model_copy(
        update={
            "role": role,
            "tier": tier,
            "backend": target.backend,
            # See the docstring: `or` is a default for an unset field. Never assign the
            # transport's resolved/served id here — it is a cache-key component.
            "model_id": usage.model_id or target.model_id,
            "cost_usd": estimate_cost_usd(target, usage.input_tokens, usage.output_tokens),
        },
    )
