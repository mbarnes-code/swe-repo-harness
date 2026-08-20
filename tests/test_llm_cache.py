"""SPEC §6 / §11.6 / ADR-0021 / ADR-0023 — the content-addressed `llm_cache`.

The cache is not an optimisation here: it is the harness's entire determinism story, because the
harness pins no sampling controls — no `temperature`, `seed`, `top_p` or `thinking` key is built
anywhere under `src/fleet/llm/` (§11.6) — and the same role may be answered by a different
transport on the next call. (The premise this docstring used to give instead, that the model's
own reasoning mode forbids pinning a temperature, was retracted as false: D66.) So each test pins
a property whose absence is *silent* — a hit that still charges, a key that changes on a patch
release, a stale answer served after the prompt changed, an LRU clock that never advances so
`fleet gc` evicts entries at random.

The inner client is a fake that counts calls and never touches a socket. "The backend was not
called" is the assertion that matters: a cache that returns the right value by calling the model
again is not a cache.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest
from pydantic import BaseModel

from fleet.llm.cache import (
    EMPTY_SHA256,
    CacheKeyParts,
    CacheMiss,
    CacheRow,
    CachingModelClient,
    MemoryLlmCacheStore,
    SqliteLlmCacheStore,
)
from fleet.llm.calls import prompt_sha256, render_prompt
from fleet.llm.client import (
    CallBudget,
    LlmError,
    Message,
    ModelCapabilities,
    ModelResponse,
    StreamEvent,
)
from fleet.llm.roles import LlmRouter, Role
from fleet.llm.schemas import PrTitle, RepoClassification, response_schema_sha256
from fleet.models.enums import ContextPolicy, ModelTier, StructuredOutputMode
from fleet.models.tasks import BackendTarget, LlmCallRecord, Price, TokenUsage
from fleet.state.db import StateWriter, connect_ro, initialize_database
from fleet.state.db import _release_write_slot as release_write_slot

ROLE = str(Role.REPO_CLASSIFY)
T0 = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)
ANSWER = RepoClassification(
    ecosystem="maven", is_library=True, confidence=0.9, rationale="pom.xml declares a groupId"
)


def target(backend: str, model_id: str, *, effort: str = "low") -> BackendTarget:
    return BackendTarget(
        backend=backend,
        model_id=model_id,
        effort=effort,
        price=Price(in_per_mtok=1.0, out_per_mtok=5.0),
    )


PRIMARY = target("anthropic", "cheap-a")
STANDBY = target("openai_compatible", "local-cheap", effort="medium")


def router() -> LlmRouter:
    return LlmRouter(
        {ROLE: ModelTier.CHEAP},
        {ModelTier.CHEAP: (PRIMARY, STANDBY)},
        required_roles=(),
    )


class FakeClient:
    """A `ModelClient` that counts calls and returns a canned, already-validated answer. It has no
    transport at all: if the cache ever misses when it should hit, `calls` says so."""

    def __init__(self, answered_by: BackendTarget = PRIMARY, *, value: BaseModel = ANSWER) -> None:
        self.calls: list[str] = []
        self._answered_by = answered_by
        self._value = value

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
        self.calls.append(role)
        return ModelResponse(
            value=response_model.model_validate_json(self._value.model_dump_json()),
            usage=TokenUsage(
                role=role,
                tier=ModelTier.CHEAP,
                backend=self._answered_by.backend,
                model_id=self._answered_by.model_id,
                input_tokens=1_000,
                output_tokens=200,
                cost_usd=0.002,
            ),
            mode=StructuredOutputMode.JSON_SCHEMA,
            finish_reason="stop",
        )

    def stream[T: BaseModel](
        self,
        role: str,
        messages: Sequence[Message],
        response_model: type[T],
        *,
        budget: CallBudget | None = None,
    ) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError

    async def capabilities(self, role: str) -> ModelCapabilities:
        return ModelCapabilities()


class Clock:
    """A hand-cranked clock: `last_hit_at` advancing must be asserted, not raced against."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, minutes: int) -> None:
        self.now += timedelta(minutes=minutes)


MESSAGES = render_prompt(Role.REPO_CLASSIFY, {"repo_id": "acme-commons", "files": ["pom.xml"]})


def build(
    *,
    store: MemoryLlmCacheStore | None = None,
    inner: FakeClient | None = None,
    clock: Clock | None = None,
    **kwargs: object,
) -> tuple[CachingModelClient, FakeClient, MemoryLlmCacheStore, Clock]:
    # `is None`, not `or`: an empty MemoryLlmCacheStore is falsy (it defines `__len__`), so `or`
    # silently hands every test a fresh store and every "it hit" assertion becomes vacuous.
    the_store = MemoryLlmCacheStore() if store is None else store
    the_inner = FakeClient() if inner is None else inner
    the_clock = Clock() if clock is None else clock
    client = CachingModelClient(
        the_inner, router(), the_store, now=the_clock, **kwargs  # type: ignore[arg-type]
    )
    return client, the_inner, the_store, the_clock


def call(client: CachingModelClient, model: type[BaseModel] = RepoClassification) -> object:
    return asyncio.run(client.complete(ROLE, MESSAGES, model))


# ---------------------------------------------------------------------------------------------
# The hit
# ---------------------------------------------------------------------------------------------


def test_a_hit_returns_the_same_validated_object_and_spends_nothing() -> None:
    """The two halves that make a cache a cache. Same object: a replayed answer must be the same
    validated Pydantic value, not a dict or a re-parsed approximation, or callers would have to
    know which they got. Spends nothing: the backend is NOT called and `cost_usd` is 0.0 — a cache
    that returns the right value by asking the model again has bought nothing at all."""
    client, inner, _, _ = build()
    first = asyncio.run(client.complete(ROLE, MESSAGES, RepoClassification))
    second = asyncio.run(client.complete(ROLE, MESSAGES, RepoClassification))

    assert inner.calls == [ROLE], "the second call must not reach the backend"
    assert second.value == first.value
    assert second.usage.cost_usd == 0.0
    assert first.usage.cost_usd > 0.0
    assert second.mode is first.mode, "a hit is indistinguishable from a fresh call but for cost"
    assert second.finish_reason == "stop"


def test_a_hit_is_reported_out_of_band_because_zero_cost_is_not_a_cache_signal() -> None:
    """§11.2/§11.6: a locally-served target legitimately costs 0.0, so `cost_usd == 0` cannot mean
    "cached". That is why §6 has an `attempts.llm_cache_hit` column, and why the hit is announced
    through a callback rather than inferred."""
    seen: list[LlmCallRecord] = []
    client, _, _, _ = build(on_hit=seen.append)
    call(client)
    assert seen == []
    call(client)
    assert [r.role for r in seen] == [ROLE]


def test_last_hit_at_advances_on_a_hit() -> None:
    """`fleet gc --cache-max-age` evicts by `last_hit_at` through `ix_llm_cache_lru` (§6, §10). An
    entry served daily whose clock never moves is deleted while it is still useful, and the only
    symptom is a cache that quietly stops helping."""
    clock = Clock()
    client, _, store, _ = build(clock=clock)
    call(client)
    key = asyncio.run(_only_key(store))
    written = asyncio.run(store.get(key))
    assert written is not None and written.last_hit_at == T0

    clock.advance(90)
    call(client)
    after = asyncio.run(store.get(key))
    assert after is not None
    assert after.last_hit_at == T0 + timedelta(minutes=90)
    assert after.record.hit_count == 1


async def _only_key(store: MemoryLlmCacheStore) -> str:
    keys = list(store._rows)  # the store's own key set, read directly for assertions
    assert len(keys) == 1
    return keys[0]


# ---------------------------------------------------------------------------------------------
# What is, and is not, in the key
# ---------------------------------------------------------------------------------------------


def test_harness_version_is_not_a_cache_key_component() -> None:
    """THE regression this module exists to prevent. `harness_version` changes on every patch
    release; keying on it meant a bug-fix release re-paid for every cached call across a 250-repo
    fleet — a full corpus re-spend caused by a version bump no model could have seen. Asserted
    both at the key and end-to-end, because the exclusion is only worth anything if the entry
    written by the old version still hits under the new one."""
    store = MemoryLlmCacheStore()
    old, inner, _, _ = build(store=store, harness_version="0.1.0")
    call(old)
    assert inner.calls == [ROLE]

    new, _, _, _ = build(store=store, inner=inner, harness_version="0.1.1")
    hit = asyncio.run(new.complete(ROLE, MESSAGES, RepoClassification))
    assert inner.calls == [ROLE], "a patch release must not re-pay for a cached call"
    assert hit.usage.cost_usd == 0.0

    parts = _parts()
    assert parts.compute() == parts.model_copy(update={"harness_version": "9.9.9"}).compute()


def _parts(**overrides: object) -> CacheKeyParts:
    base = CacheKeyParts(
        role=ROLE,
        tier=ModelTier.CHEAP,
        backend=PRIMARY.backend,
        model_id=PRIMARY.model_id,
        effort="low",
        prompt_sha256=prompt_sha256(MESSAGES),
        response_schema_sha256=response_schema_sha256(RepoClassification),
    )
    return base.model_copy(update=overrides) if overrides else base


def test_bumping_the_prompt_template_version_misses() -> None:
    """The hand-turned invalidation knob (§5). If it did not miss, a template whose MEANING
    changed would keep serving answers to the old question forever."""
    store = MemoryLlmCacheStore()
    v1, inner, _, _ = build(store=store, template_versions={ROLE: 1})
    call(v1)
    v2, _, _, _ = build(store=store, inner=inner, template_versions={ROLE: 2})
    call(v2)
    assert inner.calls == [ROLE, ROLE]

    again, _, _, _ = build(store=store, inner=inner, template_versions={ROLE: 2})
    call(again)
    assert inner.calls == [ROLE, ROLE], "the same version must still hit"


def test_changing_the_response_schema_misses() -> None:
    """The other half of §5's "what actually invalidates a cached answer". A stored reply that
    validated against the old schema may not even parse into the new one, and serving it would
    hand a caller an object its own type no longer describes."""
    store = MemoryLlmCacheStore()
    client, inner, _, _ = build(store=store, inner=FakeClient(value=ANSWER))
    call(client)

    other = FakeClient(value=PrTitle(title="migrate acme-commons"))
    titled, _, _, _ = build(store=store, inner=other)
    asyncio.run(titled.complete(ROLE, MESSAGES, PrTitle))
    assert other.calls == [ROLE], "a different response schema is a different call"
    assert inner.calls == [ROLE]

    assert (
        _parts().compute()
        != _parts(response_schema_sha256=response_schema_sha256(PrTitle)).compute()
    )


def test_a_changed_prompt_misses_so_a_stale_answer_cannot_be_applied_to_a_new_file() -> None:
    """`prompt_sha256` covers the fully rendered prompt, file content included (§11.6): any input
    change is a new key. Without it, yesterday's patch would be applied to today's file."""
    client, inner, _, _ = build()
    call(client)
    other = render_prompt(
        Role.REPO_CLASSIFY, {"repo_id": "acme-commons", "files": ["build.gradle"]}
    )
    asyncio.run(client.complete(ROLE, other, RepoClassification))
    assert inner.calls == [ROLE, ROLE]


def test_the_anti_anchoring_scope_is_part_of_the_calls_identity() -> None:
    """ADR-0021. Two rungs can render byte-identical prompts when the refutation set is empty, so
    `prompt_sha256` alone would collide a fresh-slate `EVIDENCE_ONLY` call with a priors-primed
    one — and the harness would serve a primed answer to a call whose entire purpose was to have
    no priors, silently reintroducing the anchoring the amendment removes."""
    store = MemoryLlmCacheStore()
    base, inner, _, _ = build(store=store)
    fresh = base.scoped(context_policy=ContextPolicy.EVIDENCE_ONLY)
    primed = base.scoped(
        context_policy=ContextPolicy.EVIDENCE_PLUS_REJECTED_APPROACHES,
        rejected_approach_digest="b" * 64,
    )
    call(fresh)
    call(primed)
    assert inner.calls == [ROLE, ROLE]

    call(fresh)
    assert inner.calls == [ROLE, ROLE], "the same scope must still hit"
    assert _parts().rejected_approach_digest == EMPTY_SHA256


def test_a_failover_answer_is_stored_under_the_target_that_actually_answered() -> None:
    """§13 row 39. The cache is not scoped to `run_id`, so storing a standby's answer under the
    primary's key would serve a small local model's output — silently and forever — to every later
    call the operator routed to the frontier target."""
    store = MemoryLlmCacheStore()
    failed_over = FakeClient(answered_by=STANDBY)
    client, _, _, _ = build(store=store, inner=failed_over)
    call(client)

    healthy = FakeClient(answered_by=PRIMARY)
    again, _, _, _ = build(store=store, inner=healthy)
    call(again)
    assert healthy.calls == [ROLE], "the primary's key must not hold the standby's answer"

    key = _parts(backend=STANDBY.backend, model_id=STANDBY.model_id, effort="medium").compute()
    stored = asyncio.run(store.get(key))
    assert stored is not None
    assert stored.record.backend == STANDBY.backend


# ---------------------------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------------------------


def test_read_only_turns_a_miss_into_a_hard_error() -> None:
    """Replay mode's whole value (§11.6): a miss must be fatal, or "this re-run used no new model
    output" is an assertion nobody can check. A warning-and-call would make the claim false and
    leave no trace."""
    client, inner, _, _ = build(mode="read-only")
    with pytest.raises(CacheMiss) as excinfo:
        call(client)
    assert ROLE in str(excinfo.value)
    assert inner.calls == []


def test_cache_miss_is_not_an_llm_error() -> None:
    """Pins the class hierarchy the Rule 11 fix depends on — `CacheMiss` is not a subclass of
    `LlmError` — and reproduces the bare `except LlmError:` pattern in isolation to show that
    shape alone lets a `CacheMiss` pass through it. It does NOT construct or run a worker, so
    none of `buildverify.py`, `buildgen.py` or `prwriter.py`'s real advice-call sites execute
    here; a regression in one of those specific `except` clauses (widened to `except (LlmError,
    CacheMiss):`, say) would not be caught by this test. The worker-level regression —
    `BuildverifyWorker.run()` actually raising `CacheMiss` out of `_diagnose` instead of
    swallowing it — is
    `tests/test_workers_build.py::test_a_read_only_cache_miss_during_diagnosis_is_not_swallowed_by_the_advisory_catch`."""
    assert not issubclass(CacheMiss, LlmError)

    client, inner, _, _ = build(mode="read-only")
    try:
        call(client)
    except LlmError:
        pytest.fail(
            "CacheMiss must not be swallowed by a bare `except LlmError:` advisory catch"
        )
    except CacheMiss:
        pass
    assert inner.calls == []


def test_read_only_replays_an_entry_written_by_an_earlier_run() -> None:
    store = MemoryLlmCacheStore()
    writer_client, inner, _, _ = build(store=store)
    call(writer_client)
    replay, _, _, _ = build(store=store, inner=inner, mode="read-only")
    replayed = asyncio.run(replay.complete(ROLE, MESSAGES, RepoClassification))
    assert replayed.value == ANSWER
    assert inner.calls == [ROLE]


def test_off_neither_reads_nor_writes() -> None:
    """`--llm-cache off` is for deliberately re-rolling a decision. If it wrote, the re-roll would
    poison the next run's cache with the answer the operator was trying to escape."""
    store = MemoryLlmCacheStore()
    client, inner, _, _ = build(store=store, mode="off")
    call(client)
    call(client)
    assert inner.calls == [ROLE, ROLE]
    assert len(store) == 0


# ---------------------------------------------------------------------------------------------
# The key formula and the store
# ---------------------------------------------------------------------------------------------


def test_the_key_is_the_documented_component_list_in_order() -> None:
    """§6 spells the formula out; a component quietly dropped is a whole class of wrong answers,
    and every one of them looks like a legitimate hit."""
    import hashlib

    parts = _parts(
        context_policy=ContextPolicy.EVIDENCE_ONLY,
        adapter_versions=("maven:3", "npm:2"),
        prompt_template_version=4,
    )
    expected = hashlib.sha256(
        "|".join(
            [
                ROLE,
                "CHEAP",
                PRIMARY.backend,
                PRIMARY.model_id,
                "low",
                "EVIDENCE_ONLY",
                EMPTY_SHA256,
                prompt_sha256(MESSAGES),
                "4",
                response_schema_sha256(RepoClassification),
                "maven:3,npm:2",
            ]
        ).encode()
    ).hexdigest()
    assert parts.compute() == expected


def test_every_key_component_changes_the_key() -> None:
    """Each of these changes the IDENTITY of the call (ADR-0021/0023). A component that did not
    move the key would be a component that is not really in it."""
    base = _parts().compute()
    mutations: dict[str, object] = {
        "role": "pr_title",
        "tier": ModelTier.HEAVY,
        "backend": "bedrock",
        "model_id": "other",
        "effort": "high",
        "context_policy": ContextPolicy.EVIDENCE_PLUS_PRIORS,
        "rejected_approach_digest": "c" * 64,
        "prompt_sha256": "d" * 64,
        "prompt_template_version": 7,
        "response_schema_sha256": "e" * 64,
        "adapter_versions": ("maven:9",),
    }
    for field, value in mutations.items():
        assert _parts(**{field: value}).compute() != base, f"{field} is not in the key"


def test_a_re_derived_entry_counts_as_a_hit_rather_than_overwriting_the_answer() -> None:
    """§6's idempotent-write rule for this table. Two runs deriving the same key concurrently must
    not race to replace each other's stored answer — the value is content-addressed, so the second
    write has nothing new to say."""
    store = MemoryLlmCacheStore()
    row = CacheRow(
        record=LlmCallRecord(
            cache_key=_parts().compute(),
            role=ROLE,
            tier=ModelTier.CHEAP,
            backend=PRIMARY.backend,
            model_id=PRIMARY.model_id,
            structured_output_mode=StructuredOutputMode.JSON_SCHEMA,
            effort="low",
            prompt_sha256=prompt_sha256(MESSAGES),
            response_schema_sha256=response_schema_sha256(RepoClassification),
            response_json=ANSWER.model_dump_json(),
            created_at=T0,
        ),
        last_hit_at=T0,
    )
    asyncio.run(store.put(row))
    asyncio.run(store.put(row.model_copy(update={"last_hit_at": T0 + timedelta(hours=1)})))
    stored = asyncio.run(store.get(row.record.cache_key))
    assert stored is not None
    assert stored.record.hit_count == 1
    assert stored.last_hit_at == T0 + timedelta(hours=1)


def test_eviction_is_by_last_hit_at_not_by_creation() -> None:
    """`fleet gc --cache-max-age 30d` (§10). Evicting by `created_at` would delete the entries the
    fleet uses most, which are precisely the oldest ones."""
    store = MemoryLlmCacheStore()
    client, _, _, clock = build(store=store, clock=Clock())
    call(client)
    clock.advance(60 * 24 * 40)  # 40 days later, and used again
    call(client)
    evicted = asyncio.run(store.evict_older_than(T0 + timedelta(days=30)))
    assert evicted == 0
    assert len(store) == 1


# ---------------------------------------------------------------------------------------------
# The SQLite-backed store
# ---------------------------------------------------------------------------------------------


@pytest.mark.integration
def test_the_sqlite_store_round_trips_through_the_single_writer(tmp_path: Path) -> None:
    """The durable half. `last_hit_at` and `hit_count` must survive a process boundary, or the LRU
    policy is a property of one run's memory rather than of the table §6 declares. Writes go
    through `StateWriter` — a second writer would violate §11.5."""

    async def drive() -> None:
        db = tmp_path / "state" / "fleet.db"
        await initialize_database(db)
        async with StateWriter(db, owner="llm-cache-test") as writer:
            read = await connect_ro(db)
            try:
                store = SqliteLlmCacheStore(writer=writer, read_conn=read)
                client = CachingModelClient(
                    FakeClient(), router(), store, now=Clock(), harness_version="0.1.0"
                )
                first = await client.complete(ROLE, MESSAGES, RepoClassification)
                second = await client.complete(ROLE, MESSAGES, RepoClassification)
                assert second.value == first.value
                assert second.usage.cost_usd == 0.0

                row = await store.get(_parts().compute())
                assert row is not None
                assert row.record.hit_count == 1
                assert row.record.context_policy is None
                assert row.record.usage.input_tokens == 1_000
                assert await store.evict_older_than(T0 + timedelta(days=1)) == 1
                assert await store.get(_parts().compute()) is None
            finally:
                await read.close()

    try:
        asyncio.run(drive())
    finally:
        release_write_slot()


@pytest.mark.integration
def test_no_declared_effort_persists_as_empty_string_in_a_not_null_check_free_column(
    tmp_path: Path,
) -> None:
    """ADR-0075 consequence 2 — the semantic the `effort` column annotation states, exercised.

    ADR-0075 made `BackendTarget.effort` optional and chose to spell absence as `''` in this
    column precisely so the column could stay `TEXT NOT NULL` with **no** `CHECK` and need no
    migration. `schema.sql` and §6's listing of it both carry that as a comment; a comment is not
    enforcement, and the drift that prompted this test was one of the two carrying it and the
    other not.

    Bound by exercising the semantic against the real schema rather than by string-comparing the
    two listings: a reflow of either comment would fail a text comparison while changing nothing,
    and — the direction that matters — a `CHECK (effort IN ('low','medium','high'))` added to
    `schema.sql` would sail through a text comparison of the *comment* while making a
    no-effort target unstorable. Each clause of the annotation gets its own assertion, because
    each fails differently: `''` not written (a fabricated default is back), `''` not accepted
    (a CHECK arrived), `NULL` accepted (a second spelling of absence arrived, which is the
    migration the ADR declined).
    """

    async def drive() -> None:
        db = tmp_path / "state" / "fleet.db"
        await initialize_database(db)
        async with StateWriter(db, owner="llm-cache-effort-test") as writer:
            read = await connect_ro(db)
            try:
                store = SqliteLlmCacheStore(writer=writer, read_conn=read)
                key = _parts(effort=None).compute()
                await store.put(
                    CacheRow(
                        record=LlmCallRecord(
                            cache_key=key,
                            role=ROLE,
                            tier=ModelTier.CHEAP,
                            backend=PRIMARY.backend,
                            model_id=PRIMARY.model_id,
                            structured_output_mode=StructuredOutputMode.JSON_SCHEMA,
                            effort=None,
                            prompt_sha256=prompt_sha256(MESSAGES),
                            response_schema_sha256=response_schema_sha256(RepoClassification),
                            response_json=ANSWER.model_dump_json(),
                            created_at=T0,
                        ),
                        last_hit_at=T0,
                    )
                )

                sql = "SELECT effort, typeof(effort) FROM llm_cache WHERE cache_key = ?"
                async with read.execute(sql, (key,)) as cursor:
                    stored = await cursor.fetchone()
                assert stored is not None, "the row the CHECK-free column was supposed to accept"
                assert stored[0] == "", (
                    "absence must persist as '' — a level substituted here is the fabricated "
                    "default ADR-0075 exists to remove, and it re-keys the cache"
                )
                assert stored[1] == "text", "'' is the empty TEXT value, not NULL and not a blob"

                back = await store.get(key)
                assert back is not None
                assert back.record.effort is None, "'' must read back as 'declared none'"

                async def blank_it(conn: aiosqlite.Connection) -> None:
                    await conn.execute(
                        "UPDATE llm_cache SET effort = NULL WHERE cache_key = ?", (key,)
                    )

                with pytest.raises(sqlite3.IntegrityError):
                    await writer.submit(blank_it)
            finally:
                await read.close()

    try:
        asyncio.run(drive())
    finally:
        release_write_slot()
