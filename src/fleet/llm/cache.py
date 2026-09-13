"""The content-addressed `llm_cache` (SPEC §6, §11.6, ADR-0021/0023).

The LLM tier cannot be made deterministic by sampling parameters and the spec does not pretend
otherwise: the harness pins no sampling controls, and the same role may be answered by a
different transport on the next call. **Determinism comes from caching, not sampling** — which
makes this module, not a `temperature=0`, the thing that lets a run be re-run and reviewed.

**The key** (`§6` `llm_cache.cache_key`, in this exact order):

    sha256(role | tier | backend | model_id | effort | context_policy |
           rejected_approach_digest | prompt_sha256 | prompt_template_version |
           response_schema_sha256 | adapter_versions)

Every component is there because leaving it out serves the wrong answer:

* `tier | backend | model_id` (ADR-0023) — the cache is deliberately NOT scoped to `run_id`, so
  without them an answer produced by a small local model after one failover would be served,
  silently and forever, to a call the operator routed to a frontier model (§13 row 39).
* `context_policy | rejected_approach_digest` (ADR-0021) — a fresh-slate `EVIDENCE_ONLY` call and
  a priors-primed one can render byte-identical prompts when the refutation set is empty, and it
  is exactly then that they are genuinely the same call. Making that explicit is what stops the
  harness serving a primed answer to a call whose whole purpose was to have no priors.
* `prompt_template_version | response_schema_sha256` — the two things that actually invalidate a
  cached answer, and the only two invalidation knobs the harness has.

**`harness_version` is deliberately NOT a component.** It changes on every patch release, and
including it meant a bug-fix release re-paid for every cached call across a 250-repo fleet — a
full corpus re-spend triggered by a version bump that changed nothing a model can see. The field
is still carried on `CacheKeyParts` so the exclusion is visible and testable rather than being an
omission somebody re-adds by accident.

**Modes** (`--llm-cache`, §11.6): `read-write` (default), `read-only` — the replay mode, where a
miss is a hard error, which is what makes "this re-run used no new model output" a provable claim
— and `off`, for deliberately re-rolling a decision.

`last_hit_at` is maintained on every hit and is the ONLY thing `fleet gc --cache-max-age` can
evict by (`ix_llm_cache_lru`, §6/§10): `response_json` is unbounded and the table is run-unscoped,
so without the clock "stale entries age out" names no mechanism at all.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal, Protocol, final, runtime_checkable

from pydantic import BaseModel, Field

from fleet.llm.calls import prompt_sha256, prompt_template_version
from fleet.llm.client import (
    CallBudget,
    Message,
    ModelClient,
    ModelResponse,
    RoleRouter,
    StreamEvent,
    TierRoute,
)
from fleet.llm.roles import Role
from fleet.llm.schemas import response_schema_sha256
from fleet.models.base import FleetModel, utcnow
from fleet.models.enums import ContextPolicy, ModelTier, StructuredOutputMode
from fleet.models.tasks import BackendTarget, LlmCallRecord, ModelCapabilities, TokenUsage
from fleet.obs.redact import redact_text
from fleet.util.hashing import cache_key as _join_and_hash

if TYPE_CHECKING:  # pragma: no cover - typing only; the sqlite store is injected, never imported
    import aiosqlite

    from fleet.state.db import StateWriter

__all__ = [
    "EMPTY_SHA256",
    "CacheKeyParts",
    "CacheMiss",
    "CacheMode",
    "CacheRow",
    "CachingModelClient",
    "LlmCacheStore",
    "MemoryLlmCacheStore",
    "ScopedModelClient",
    "SqliteLlmCacheStore",
]

type CacheMode = Literal["read-write", "read-only", "off"]

EMPTY_SHA256: Final = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
"""`sha256(b"")` — the `rejected_approach_digest` of a call that rendered no refutations. §6 uses
it as the column default so every pre-ADR-0021 row keys identically to a fresh-slate call."""


class CacheMiss(Exception):
    """A miss under `--llm-cache read-only`. Replay mode's whole purpose is that a miss is fatal:
    a re-run that quietly called a model instead of replaying one is a re-run whose "no new model
    output" claim is false, and nothing downstream could tell.

    **Deliberately NOT an `LlmError`.** Every worker's advice-call site catches the bare
    `LlmError` family to degrade gracefully when the MODEL failed to answer (a transport hiccup,
    a malformed reply, an exhausted budget) — losing that advice must never turn a recorded
    build/repair failure into an unrecorded worker crash. A replay-integrity break is a different
    kind of failure: the model was never even asked, so there is nothing to degrade gracefully
    FROM. If `CacheMiss` subclassed `LlmError` those same bare `except LlmError:` sites would
    swallow it too, turning `--llm-cache read-only`'s one job — a hard failure on replay drift —
    into exactly the silent degradation it exists to prevent. Staying a plain `Exception` still
    satisfies Rule 11: `BaseWorker._run_one` (`workers/base.py:897`) classifies and records every
    exception that escapes a worker's `run()`, `LlmError` or not."""

    def __init__(self, role: str, key: str) -> None:
        super().__init__(
            f"llm cache miss for role {role!r} (key {key}) under --llm-cache read-only; "
            "replay requires an entry written by an earlier run",
        )
        self.role = role
        self.key = key


class CacheKeyParts(FleetModel):
    """The identity of one call. `compute()` is the §6 formula, component for component."""

    role: str = Field(min_length=1)
    tier: ModelTier
    backend: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    effort: Literal["low", "medium", "high"] | None = None
    # `None` = the target declared no effort. Keyed as "" so an unstated preference and an
    # explicit `medium` are DIFFERENT keys — collapsing them would re-introduce the
    # fabricated default this optionality exists to remove.
    context_policy: ContextPolicy | None = None
    rejected_approach_digest: str = Field(default=EMPTY_SHA256, pattern=r"^[0-9a-f]{64}$")
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_template_version: int = Field(default=1, ge=1)
    response_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapter_versions: tuple[str, ...] = ()
    harness_version: str = Field(
        default="",
        description="CARRIED, NEVER KEYED. Present so the exclusion §5 argues for is a visible "
        "decision with a test on it, rather than a missing line in `compute()` that a later "
        "edit re-adds and nobody notices until the next release re-pays for the fleet.",
    )

    def compute(self) -> str:
        """sha256 over the components, `|`-joined in §6's order. `harness_version` is absent."""
        return _join_and_hash(
            self.role,
            str(self.tier),
            self.backend,
            self.model_id,
            "" if self.effort is None else self.effort,
            "" if self.context_policy is None else str(self.context_policy),
            self.rejected_approach_digest,
            self.prompt_sha256,
            str(self.prompt_template_version),
            self.response_schema_sha256,
            ",".join(sorted(self.adapter_versions)),
        )


class CacheRow(FleetModel):
    """One `llm_cache` row: the durable `LlmCallRecord` plus the LRU clock the table keeps beside
    it. `last_hit_at` is a separate field because `LlmCallRecord` is the record of a *call* and
    the clock is a property of the *entry* — the record must not appear to change when nothing
    about the call did."""

    record: LlmCallRecord
    last_hit_at: datetime


class LlmCacheStore(Protocol):
    """Storage for `llm_cache`, injected. The client never opens a connection (Guardrail 3), so a
    test drives the real caching logic against a dict and `fleet gc` drives the same interface."""

    async def get(self, key: str) -> CacheRow | None:
        """Read WITHOUT touching the LRU clock — for `fleet models`/inspection paths."""
        ...

    async def touch(self, key: str, *, at: datetime) -> CacheRow | None:
        """Read AND stamp: `hit_count += 1`, `last_hit_at = at`, in one transaction.

        One method rather than get-then-touch on purpose: a hit that forgot to stamp the clock is
        an entry `fleet gc --cache-max-age` deletes while it is still being served daily, and the
        symptom is a cache that mysteriously stops helping."""
        ...

    async def put(self, row: CacheRow) -> None:
        """Insert, or count a re-derivation as a hit (§6's idempotent-write rule for this table:
        `DO UPDATE SET hit_count = hit_count + 1, last_hit_at = ?`). Never overwrites a stored
        answer: two runs deriving the same key must not race to replace each other's value."""
        ...

    async def evict_older_than(self, cutoff: datetime) -> int:
        """`fleet gc --cache-max-age` (§10), by `last_hit_at` via `ix_llm_cache_lru`."""
        ...


@final
class MemoryLlmCacheStore:
    """In-process store. Real enough to test the whole policy — including `last_hit_at`
    advancement and LRU eviction — with no database and no I/O."""

    __slots__ = ("_rows",)

    def __init__(self) -> None:
        self._rows: dict[str, CacheRow] = {}

    async def get(self, key: str) -> CacheRow | None:
        row = self._rows.get(key)
        return None if row is None else row.model_copy(deep=True)

    async def touch(self, key: str, *, at: datetime) -> CacheRow | None:
        row = self._rows.get(key)
        if row is None:
            return None
        updated = CacheRow(
            record=row.record.model_copy(update={"hit_count": row.record.hit_count + 1}),
            last_hit_at=at,
        )
        self._rows[key] = updated
        return updated.model_copy(deep=True)

    async def put(self, row: CacheRow) -> None:
        existing = self._rows.get(row.record.cache_key)
        if existing is not None:
            self._rows[row.record.cache_key] = CacheRow(
                record=existing.record.model_copy(
                    update={"hit_count": existing.record.hit_count + 1},
                ),
                last_hit_at=row.last_hit_at,
            )
            return
        self._rows[row.record.cache_key] = row.model_copy(deep=True)

    async def evict_older_than(self, cutoff: datetime) -> int:
        stale = [key for key, row in self._rows.items() if row.last_hit_at < cutoff]
        for key in stale:
            del self._rows[key]
        return len(stale)

    def __len__(self) -> int:
        return len(self._rows)


_COLUMNS: Final = (
    "cache_key, role, tier, backend, model_id, structured_output_mode, effort, context_policy, "
    "rejected_approach_digest, prompt_sha256, prompt_template_version, response_schema_sha256, "
    "response_json, input_tokens, output_tokens, cost_usd, hit_count, created_at, last_hit_at"
)


@final
class SqliteLlmCacheStore:
    """`llm_cache` over the §11.5 single writer.

    Both collaborators are injected exactly as `SqliteStateRepository` takes them: reads go to the
    `mode=ro` handle and every write goes through `StateWriter`, so this class cannot become a
    second writer. Each submitted unit is pure SQL — the model call happens outside it, which is
    the write-transaction contract `state/db.py` states and this module must not be the one to
    break (an LLM call inside a write transaction parks the fleet's only write lock).
    """

    __slots__ = ("_read", "_writer")

    def __init__(self, *, writer: StateWriter, read_conn: aiosqlite.Connection) -> None:
        self._writer = writer
        self._read = read_conn

    async def get(self, key: str) -> CacheRow | None:
        sql = f"SELECT {_COLUMNS} FROM llm_cache WHERE cache_key = ?"  # noqa: S608 - fixed names
        async with self._read.execute(sql, (key,)) as cursor:
            row = await cursor.fetchone()
        return None if row is None else _row_to_cache_row(row)

    async def touch(self, key: str, *, at: datetime) -> CacheRow | None:
        stamp = at.isoformat()
        select_sql = f"SELECT {_COLUMNS} FROM llm_cache WHERE cache_key = ?"  # noqa: S608
        update_sql = (
            "UPDATE llm_cache SET hit_count = hit_count + 1, last_hit_at = ? WHERE cache_key = ?"
        )

        async def unit(conn: aiosqlite.Connection) -> CacheRow | None:
            await conn.execute(update_sql, (stamp, key))
            async with conn.execute(select_sql, (key,)) as cursor:
                row = await cursor.fetchone()
            return None if row is None else _row_to_cache_row(row)

        return await self._writer.submit(unit)

    async def put(self, row: CacheRow) -> None:
        record = row.record
        # `_COLUMNS` is a module constant of column names; every VALUE is a bound parameter.
        sql = (
            f"INSERT INTO llm_cache ({_COLUMNS}) "  # noqa: S608
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (cache_key) DO UPDATE SET hit_count = hit_count + 1, "
            "    last_hit_at = excluded.last_hit_at"
        )
        params = (
            record.cache_key,
            record.role,
            str(record.tier),
            record.backend,
            record.model_id,
            str(record.structured_output_mode),
            "" if record.effort is None else record.effort,
            None if record.context_policy is None else str(record.context_policy),
            record.rejected_approach_digest,
            record.prompt_sha256,
            record.prompt_template_version,
            record.response_schema_sha256,
            record.response_json,
            record.usage.input_tokens,
            record.usage.output_tokens,
            record.usage.cost_usd,
            record.hit_count,
            record.created_at.isoformat(),
            row.last_hit_at.isoformat(),
        )

        async def unit(conn: aiosqlite.Connection) -> None:
            await conn.execute(sql, params)

        await self._writer.submit(unit)

    async def evict_older_than(self, cutoff: datetime) -> int:
        stamp = cutoff.isoformat()

        async def unit(conn: aiosqlite.Connection) -> int:
            cursor = await conn.execute("DELETE FROM llm_cache WHERE last_hit_at < ?", (stamp,))
            return int(cursor.rowcount)

        return await self._writer.submit(unit)


def _row_to_cache_row(row: Sequence[object]) -> CacheRow:
    """One `llm_cache` row → typed. Explicit per-column casts, as `state/repository.py` does:
    SQLite is dynamically typed, so an untyped tuple would let a text `cost_usd` through."""
    policy = row[7]
    return CacheRow(
        record=LlmCallRecord(
            cache_key=str(row[0]),
            role=str(row[1]),
            tier=ModelTier(str(row[2])),
            backend=str(row[3]),
            model_id=str(row[4]),
            structured_output_mode=StructuredOutputMode(str(row[5])),
            effort=_as_effort(str(row[6])),
            context_policy=None if policy is None else ContextPolicy(str(policy)),
            rejected_approach_digest=str(row[8]),
            prompt_sha256=str(row[9]),
            prompt_template_version=int(str(row[10])),
            response_schema_sha256=str(row[11]),
            response_json=str(row[12]),
            usage=TokenUsage(
                role=str(row[1]),
                tier=ModelTier(str(row[2])),
                backend=str(row[3]),
                model_id=str(row[4]),
                input_tokens=int(str(row[13])),
                output_tokens=int(str(row[14])),
                cost_usd=float(str(row[15])),
            ),
            hit_count=int(str(row[16])),
            created_at=datetime.fromisoformat(str(row[17])),
        ),
        last_hit_at=datetime.fromisoformat(str(row[18])),
    )


def _as_effort(value: str) -> Literal["low", "medium", "high"] | None:
    """Narrow a TEXT column to the literal, or to `None` for a target that declared no effort.

    Written as comparisons rather than a cast: SQLite would happily hand back `effort = 'HIGH'`,
    and a cast would let it through. `""` is the stored spelling of absence — the column is
    `TEXT NOT NULL`, so absence is an empty string rather than a NULL and no migration is needed;
    what matters for the cache key is only that it is distinct from every real level.
    """
    if value == "":
        return None
    if value == "low":
        return "low"
    if value == "medium":
        return "medium"
    if value == "high":
        return "high"
    raise ValueError(f"llm_cache.effort holds {value!r}, not one of low/medium/high or ''")


@runtime_checkable
class ScopedModelClient(Protocol):
    """Structural narrowing of `ModelClient` for callers needing ADR-0021 per-rung cache scoping
    (D137). `CachingModelClient` below satisfies this structurally with no explicit inheritance;
    a bare test fake (or any `ModelClient` that isn't cache-backed) does not, and a caller must
    degrade to the unscoped client rather than requiring every fake to grow a `.scoped()` it has
    no use for. Deliberately NOT a widening of `ModelClient` itself — `complete()`'s signature
    must not grow an anti-anchoring parameter every backend and fake would have to care about
    (see `scoped()`'s own docstring below)."""

    def scoped(
        self,
        *,
        context_policy: ContextPolicy | None,
        rejected_approach_digest: str = EMPTY_SHA256,
    ) -> ModelClient: ...


@final
class CachingModelClient:
    """A `ModelClient` that answers from `llm_cache` when it can (§11.6).

    A decorator, not a subclass: it satisfies the same §7.7 protocol as the client it wraps, so
    every caller — worker, ladder rung, `fleet models` — is unchanged, and removing the cache is
    deleting one construction. **A hit is indistinguishable from a fresh call at this boundary
    except for cost**: the same validated object, the same `mode`, a `ModelResponse` of the same
    shape. Cost is the one intended difference (`cost_usd = 0.0`, §11.6), and because a local
    profile legitimately answers at `0.0` too, the cache *signal* is reported separately from
    cost — which is exactly why §6 has an `attempts.llm_cache_hit` column and not a
    `cost_usd == 0` convention.

    That signal travels on `TokenUsage.llm_cache_lookups` / `llm_cache_hits`, set on BOTH sides
    of the lookup below, because it has to reach a row this class cannot see: one client serves a
    whole wave, `LlmCallRecord` carries no `run_id`/`repo_id`/`phase`/`attempt`, and the usage is
    the only thing that already flows from the call to the attempt that made it. `on_hit` is
    retained and unchanged — it is an out-of-band observer hook (`scoped()` propagates it), not
    the column's route.
    """

    def __init__(
        self,
        inner: ModelClient,
        router: RoleRouter,
        store: LlmCacheStore,
        *,
        mode: CacheMode = "read-write",
        template_versions: Mapping[str, int] | None = None,
        context_policy: ContextPolicy | None = None,
        rejected_approach_digest: str = EMPTY_SHA256,
        adapter_versions: Sequence[str] = (),
        harness_version: str = "",
        on_hit: Callable[[LlmCallRecord], None] | None = None,
        now: Callable[[], datetime] = utcnow,
        redact: Callable[[str], str] = redact_text,
    ) -> None:
        self._inner = inner
        self._router = router
        self._store = store
        self._mode = mode
        self._template_versions = template_versions
        self._context_policy = context_policy
        self._rejected_approach_digest = rejected_approach_digest
        self._adapter_versions = tuple(adapter_versions)
        self._harness_version = harness_version
        self._on_hit = on_hit
        self._now = now
        self._redact = redact

    def scoped(
        self,
        *,
        context_policy: ContextPolicy | None,
        rejected_approach_digest: str = EMPTY_SHA256,
    ) -> CachingModelClient:
        """A view of this client for one ladder rung (ADR-0021).

        The policy and the digest of the refutations **actually rendered** are per-call key
        components, but `ModelClient.complete()` has no parameter for them and must not grow one —
        widening the protocol for the cache's benefit would make every backend and every fake care
        about anti-anchoring. A rung binds them here instead and hands the result to code that
        still only knows `ModelClient`.
        """
        return CachingModelClient(
            self._inner,
            self._router,
            self._store,
            mode=self._mode,
            template_versions=self._template_versions,
            context_policy=context_policy,
            rejected_approach_digest=rejected_approach_digest,
            adapter_versions=self._adapter_versions,
            harness_version=self._harness_version,
            on_hit=self._on_hit,
            now=self._now,
            redact=self._redact,
        )

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
        """See `ModelClient.complete`. Lookup, then delegate, then store."""
        if self._mode == "off":
            return await self._inner.complete(
                role,
                messages,
                response_model,
                tier_override=tier_override,
                max_output_tokens=max_output_tokens,
                timeout_s=timeout_s,
                budget=budget,
            )

        route = self._router.resolve(role, tier_override=tier_override)
        schema_sha = response_schema_sha256(response_model)
        parts = self._key_parts(role, route.tier, route.targets[0], messages, schema_sha)
        key = parts.compute()

        row = await self._store.touch(key, at=self._now())
        if row is not None:
            return self._replay(row, response_model)
        if self._mode == "read-only":
            raise CacheMiss(role, key)

        response = await self._inner.complete(
            role,
            messages,
            response_model,
            tier_override=tier_override,
            max_output_tokens=max_output_tokens,
            timeout_s=timeout_s,
            budget=budget,
        )
        await self._store_response(role, route, parts, response, schema_sha)
        # The MISS half of the §11.6 signal, and the reason the flag can be ALL-hit at all: a usage
        # that only ever recorded hits cannot distinguish "one call, cached" from "one hit and one
        # miss", and `accumulate` would fold the two identically. Stamped AFTER `_store_response`
        # on purpose — a row persisted with `llm_cache_lookups = 1` would replay a phantom second
        # lookup on every future hit. `mode == "off"` returns far above and stamps nothing: it
        # consulted no cache, so it reports no lookup and `all_served_from_llm_cache` is `False`
        # rather than vacuously true.
        return response.model_copy(
            update={
                "usage": response.usage.model_copy(
                    update={"llm_cache_lookups": response.usage.llm_cache_lookups + 1},
                ),
            },
        )

    def stream[T: BaseModel](
        self,
        role: str,
        messages: Sequence[Message],
        response_model: type[T],
        *,
        budget: CallBudget | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Progress events are a property of a live call; a replayed answer has none to forward,
        so streaming is delegated untouched rather than faked."""
        return self._inner.stream(role, messages, response_model, budget=budget)

    async def capabilities(self, role: str) -> ModelCapabilities:
        return await self._inner.capabilities(role)

    # -- internals -----------------------------------------------------------------------------

    def _key_parts(
        self,
        role: str,
        tier: ModelTier,
        target: BackendTarget,
        messages: Sequence[Message],
        schema_sha: str,
    ) -> CacheKeyParts:
        return CacheKeyParts(
            role=role,
            tier=tier,
            backend=target.backend,
            model_id=target.model_id,
            effort=target.effort,
            context_policy=self._context_policy,
            rejected_approach_digest=self._rejected_approach_digest,
            prompt_sha256=prompt_sha256(messages),
            prompt_template_version=self._template_version(role),
            response_schema_sha256=schema_sha,
            adapter_versions=self._adapter_versions,
            harness_version=self._harness_version,
        )

    def _template_version(self, role: str) -> int:
        """The role's `prompt_template_version`. Injected mapping first, then `calls.PROMPTS` —
        a role with no template (a caller assembling its own messages) keys at version 1 rather
        than raising, because the prompt hash already covers what it actually sent."""
        if self._template_versions is not None:
            return self._template_versions.get(role, 1)
        try:
            return prompt_template_version(Role(role))
        except ValueError:
            return 1

    def _replay[T: BaseModel](self, row: CacheRow, response_model: type[T]) -> ModelResponse[T]:
        """A stored answer, re-validated. Re-validated rather than trusted: the row is JSON on
        disk that an operator, a migration or a corrupted page can have touched since, and §7.7's
        invariant is that no unvalidated value leaves this boundary."""
        record = row.record
        if self._on_hit is not None:
            self._on_hit(record)
        return ModelResponse(
            value=response_model.model_validate_json(record.response_json),
            usage=record.usage.model_copy(
                update={"cost_usd": 0.0, "llm_cache_lookups": 1, "llm_cache_hits": 1},
            ),
            mode=record.structured_output_mode,
            finish_reason="stop",
            repairs=0,
        )

    async def _store_response[T: BaseModel](
        self,
        role: str,
        route: TierRoute,
        parts: CacheKeyParts,
        response: ModelResponse[T],
        schema_sha: str,
    ) -> None:
        """Write under the identity of the target that ACTUALLY answered.

        Failover makes this load-bearing. If a standby answered, storing under the primary's key
        would serve the standby's answer to every later call the operator routed to the primary —
        cross-backend poisoning, invisible because the served row looks like a legitimate hit
        (§13 row 39). Storing under the answering target's key instead means the next call with a
        healthy primary correctly misses, and a call that fails over the same way correctly hits.
        """
        usage = response.usage
        answered = _target_for(route, usage.backend, usage.model_id)
        stored = parts.model_copy(
            update={
                "tier": usage.tier or parts.tier,
                "backend": usage.backend or parts.backend,
                "model_id": usage.model_id or parts.model_id,
                "effort": parts.effort if answered is None else answered.effort,
                "response_schema_sha256": schema_sha,
            },
        )
        now = self._now()
        record = LlmCallRecord(
            cache_key=stored.compute(),
            prompt_template_version=stored.prompt_template_version,
            role=role,
            tier=stored.tier,
            backend=stored.backend,
            model_id=stored.model_id,
            structured_output_mode=response.mode,
            effort=stored.effort,
            context_policy=stored.context_policy,
            rejected_approach_digest=stored.rejected_approach_digest,
            prompt_sha256=stored.prompt_sha256,
            response_schema_sha256=stored.response_schema_sha256,
            # §6: "redacted before write". The one write boundary this module owns, so the scrub
            # happens here and not at review time (§11.4).
            response_json=self._redact(response.value.model_dump_json()),
            usage=usage,
            hit_count=0,
            created_at=now,
        )
        await self._store.put(CacheRow(record=record, last_hit_at=now))


def _target_for(route: TierRoute, backend: str, model_id: str) -> BackendTarget | None:
    """The `BackendTarget` that answered, so its `effort` — a key component — is the answering
    target's and not the primary's. `None` when the usage names a target outside this route, which
    a caller treats as "keep what the primary declared" rather than as an error.

    `(backend, model_id)` is a sufficient key here ONLY because `TierRoute` refuses a route that
    declares that pair twice with different `effort` (its `_one_effort_per_backend_and_model_id`
    validator, `llm/client.py`). `TokenUsage` carries no `effort`, so without that refusal the
    first match would win whatever actually answered and this function would silently hand
    `_store_response` a standby's answer under the primary's key.
    """
    for target in route.targets:
        if (target.backend, target.model_id) == (backend, model_id):
            return target
    return None


def _protocol_conformance(client: CachingModelClient) -> ModelClient:
    """`mypy --strict` proof that the decorator still satisfies §7.7's `ModelClient` — the whole
    premise of the cache being removable by deleting one construction."""
    return client
