"""SPEC §12.51 / ADR-0149 — replica endpoints of ONE logical target, with per-repo affinity.

Proven here, in the default (non-`live`) suite:

* (ii) `replica_index` is a STABLE hash — identical across two interpreters with different
  `PYTHONHASHSEED`; every call made through one `for_repo(repo_id)` view, and every round trip of
  one `complete()` (a schema repair), lands on the same endpoint.
* (i) over a real-CLI fixture run (`fleet scan`, real `ClassifyWorker`, two REAL loopback stub
  servers), both endpoints serve calls, and the per-endpoint split is recorded on the `llm_call`
  events' `base_url` field.
* (iii) with one replica refusing connections, the same run completes on the other; the hop is an
  ordinary §12.43 failover (`backend_failover` event, `usage.llm_failovers`), and no repo is
  charged for it.

NOT proven here: (i)'s LIVE half against the two real Spark hosts (a later round with hardware).

Rule 12 — the unique discriminator of each mutation (the ADR-0149 matrix):

* builtin `hash()` in `replica_index` → `test_replica_index_is_stable_across_hash_seeds`.
* per-call re-roll in `resolve_replicas` → `test_every_call_for_one_repo_hits_one_replica`.
* replica failover disabled (affine replica only) → `test_a_refusing_replica_fails_over_...`
  and the CLI arm `test_fleet_scan_completes_on_the_live_replica_...`.
* breaker keyed on `backend:model_id` alone → `test_a_refusing_replica_fails_over_...` (the peer's
  successes reset the SHARED counter, so the breaker never opens and the dead replica is dialled
  on all five calls instead of three — every call still completes).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from pydantic import BaseModel, ValidationError

from fleet.cli import ExitCode
from fleet.llm.cache import CachingModelClient
from fleet.llm.client import (
    BackendFailover,
    BackendReply,
    LadderModelClient,
    LlmCall,
    Message,
    ModelTier,
    StructuredOutputMode,
    TierRoute,
    TransportError,
    replica_index,
    resolve_replicas,
    scope_to_repo,
)
from fleet.models.enums import ContextPolicy
from fleet.models.tasks import BackendTarget, ModelCapabilities, TokenUsage
from tests.fixtures.llm.stub_openai_server import running_stub_server
from tests.test_heavy_tier_outage_e2e import (
    _NAMES,
    _classify_responder,
    _make_workspace_custom,
    scan_real_classify,
)
from tests.test_scan_e2e import query

SRC = str(Path(__file__).resolve().parents[1] / "src")

_A = "http://replica-a.invalid/v1"
_B = "http://replica-b.invalid/v1"
_URLS = (_A, _B)


class _Ok(BaseModel):
    ok: bool


def _target() -> BackendTarget:
    return BackendTarget(
        backend="replica_stub", model_id="m", price="free", base_urls=_URLS, effort="low"
    )


class _Router:
    def __init__(self, target: BackendTarget) -> None:
        self._route = TierRoute(tier=ModelTier.HEAVY, targets=(target,))

    def resolve(self, role: str, *, tier_override: ModelTier | None = None) -> TierRoute:
        return self._route


class _ReplicaBackend:
    """Records the endpoint of every `invoke()`. `dead` endpoints refuse (CONNECTION);
    `repair_once` answers the first call with an invalid reply so `complete()` re-asks."""

    name: ClassVar[str] = "replica_stub"
    version: ClassVar[int] = 1

    def __init__(self, *, dead: Sequence[str] = (), repair_once: bool = False) -> None:
        self.calls: list[str | None] = []
        self._dead = set(dead)
        self._repair_pending = repair_once

    def declared_capabilities(self, target: BackendTarget) -> ModelCapabilities:
        return ModelCapabilities(
            supports_json_schema=True,
            structured_output_modes=(StructuredOutputMode.JSON_SCHEMA,),
        )

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
        assert target.base_urls is None, "a backend must only ever see a resolved base_url"
        self.calls.append(target.base_url)
        if target.base_url in self._dead:
            raise TransportError(f"refused: {target.base_url}", trigger="CONNECTION")
        text = '{"ok": true}'
        if self._repair_pending:
            self._repair_pending = False
            text = '{"nope": 1}'
        return BackendReply(
            text=text, usage=TokenUsage(input_tokens=1, output_tokens=1), finish_reason="stop"
        )


def _client(
    backend: _ReplicaBackend,
    *,
    failovers: list[BackendFailover] | None = None,
    calls: list[LlmCall] | None = None,
) -> LadderModelClient:
    return LadderModelClient(
        _Router(_target()),
        {"replica_stub": backend},
        on_failover=None if failovers is None else failovers.append,
        on_llm_call=None if calls is None else calls.append,
    )


def _repo_on(index: int) -> str:
    """A repo id whose affine replica is `_URLS[index]` — found, not assumed."""
    return next(f"repo-{i}" for i in range(1000) if replica_index(f"repo-{i}", 2) == index)


# -------------------------------------------------------------------------------------------
# (ii) stability and affinity
# -------------------------------------------------------------------------------------------

_HASH_SCRIPT = """\
import json
from fleet.llm.client import replica_index
print(json.dumps([replica_index(f"repo-{i}", n) for n in (2, 3) for i in range(64)]))
"""


def _indices_under_seed(seed: str) -> list[int]:
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _HASH_SCRIPT],
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONHASHSEED": seed, "PYTHONPATH": SRC, "PATH": os.environ.get("PATH", "")},
    )
    return cast(list[int], json.loads(proc.stdout))


def test_replica_index_is_stable_across_hash_seeds() -> None:
    """Two interpreters, two `PYTHONHASHSEED`s, one answer — and the in-process answer agrees.
    Under builtin `hash()` the str hash is salted per seed, so the lists differ."""
    first, second = _indices_under_seed("1"), _indices_under_seed("2")
    assert first == second, "replica selection depends on PYTHONHASHSEED"
    here = [replica_index(f"repo-{i}", n) for n in (2, 3) for i in range(64)]
    assert first == here
    assert set(first[:64]) == {0, 1}, "64 repos all mapped to one of two replicas"


def test_every_call_for_one_repo_hits_one_replica() -> None:
    """(ii): twenty calls through ONE `for_repo` view land on ONE endpoint — the repo's affine
    one — and a second repo affine to the other replica lands there, also every time."""
    backend = _ReplicaBackend()
    base = _client(backend)
    for index in (0, 1):
        backend.calls.clear()
        client = base.for_repo(_repo_on(index))
        for _ in range(20):
            asyncio.run(client.complete("r", [Message(role="user", content="x")], _Ok))
        assert backend.calls == [_URLS[index]] * 20, backend.calls


async def test_every_round_trip_of_one_call_stays_on_one_replica() -> None:
    """(ii): a schema repair is a second `invoke()` inside ONE `complete()`; both go to the same
    endpoint."""
    backend = _ReplicaBackend(repair_once=True)
    client = _client(backend).for_repo(_repo_on(1))
    response = await client.complete("r", [Message(role="user", content="x")], _Ok)
    assert response.repairs == 1
    assert backend.calls == [_B, _B]


async def test_calls_distribute_and_the_split_is_recorded_per_endpoint() -> None:
    """(i), client-level: repos spread over both replicas, and `llm_call` records which endpoint
    served each call (`LlmCall.base_url`)."""
    backend = _ReplicaBackend()
    calls: list[LlmCall] = []
    base = _client(backend, calls=calls)
    for i in range(16):
        await base.for_repo(f"repo-{i}").complete("r", [Message(role="user", content="x")], _Ok)
    split = {url: sum(1 for c in calls if c.base_url == url) for url in _URLS}
    assert sum(split.values()) == 16
    assert all(split.values()), split


def test_the_registered_target_is_never_mutated() -> None:
    target = _target()
    concrete = resolve_replicas(target, _repo_on(1))
    assert [c.base_url for c in concrete] == [_B, _A]
    assert all(c.base_urls is None for c in concrete)
    assert target.base_url is None and target.base_urls == _URLS
    assert resolve_replicas(target, None)[0].base_url == _A
    single = BackendTarget(backend="x", model_id="m", price="free", base_url=_A)
    assert resolve_replicas(single, "anything") == (single,)


# -------------------------------------------------------------------------------------------
# (iii) replica failover, client-level
# -------------------------------------------------------------------------------------------


async def test_a_refusing_replica_fails_over_to_its_peer_and_is_accounted_as_a_failover() -> None:
    """(iii): the repo's affine replica refuses; five calls (more than `open_after_failures`)
    all complete on the peer, each reporting one failover hop, with a CONNECTION
    `backend_failover` per actually-refused call."""
    repo = _repo_on(0)
    backend = _ReplicaBackend(dead=(_A,))
    failovers: list[BackendFailover] = []
    client = _client(backend, failovers=failovers).for_repo(repo)
    for _ in range(5):
        response = await client.complete("r", [Message(role="user", content="x")], _Ok)
        assert response.value.ok
        assert response.usage.llm_failovers == 1
    served = [c for c in backend.calls if c == _B]
    assert len(served) == 5
    # the breaker opens after 3 refusals, so later calls skip the dead replica without dialling
    assert backend.calls.count(_A) == 3
    assert len(failovers) == 3
    assert all(f.trigger == "CONNECTION" for f in failovers)


# -------------------------------------------------------------------------------------------
# wiring: how `repo_id` reaches the ladder client
# -------------------------------------------------------------------------------------------


class _BareFake:
    async def complete(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - never called
        raise AssertionError


def test_scope_to_repo_degrades_for_a_client_without_for_repo() -> None:
    fake = _BareFake()
    assert scope_to_repo(cast(Any, fake), "repo") is fake


def test_the_caching_layer_forwards_the_binding_and_scoped_views_keep_it() -> None:
    """`CachingModelClient.for_repo` binds the INNER ladder client; a later `scoped()` rung view
    (rewrite.py's `_scoped_client`) reuses `_inner`, so the binding survives it. The breaker is
    shared with the unbound client — a view, not a second client."""
    ladder = _client(_ReplicaBackend())
    caching = CachingModelClient(ladder, _Router(_target()), cast(Any, None))
    bound = caching.for_repo("repo-7")
    rung = bound.scoped(context_policy=ContextPolicy.EVIDENCE_ONLY)
    for view in (bound, rung):
        inner = cast(LadderModelClient, view._inner)
        assert inner._repo_id == "repo-7"
        assert inner._health is ladder._health
    assert ladder._repo_id is None


# -------------------------------------------------------------------------------------------
# schema
# -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        {"base_urls": (_A,)},
        {"base_urls": (_A, _A)},
        {"base_urls": (_A, " ")},
        {"base_urls": _URLS, "base_url": _A},
    ],
)
def test_base_urls_is_two_or_more_distinct_endpoints_and_excludes_base_url(
    fields: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        BackendTarget.model_validate({"backend": "x", "model_id": "m", "price": "free", **fields})


# -------------------------------------------------------------------------------------------
# (i) and (iii) through the real CLI, over two real loopback stub servers
# -------------------------------------------------------------------------------------------

_MODELS_YAML = """\
version: 2
roles:
  conflict_resolution: HEAVY
  api_incompat_rewrite: HEAVY
  build_authoring: HEAVY
  cycle_break_proposal: HEAVY
  escalation: HEAVY
  transform_repair: WORKHORSE
  build_diagnosis: WORKHORSE
  manifest_extract: WORKHORSE
  pr_body: WORKHORSE
  repo_classify: HEAVY
  dep_disambiguate: CHEAP
  pr_title: CHEAP
default_profile: default
profiles:
  default:
    HEAVY:
      - {{ backend: openai_compatible, model_id: fixture-heavy, effort: high, price: free,
          base_urls: ['{a}', '{b}'],
          capabilities_override: {{ supports_json_schema: true, max_output_tokens: 2048,
                                   structured_output_modes: [JSON_SCHEMA, PROMPTED] }} }}
    WORKHORSE:
      - {{ backend: anthropic, model_id: claude-sonnet-5, effort: high,
          api_key_env: ANTHROPIC_API_KEY,
          price: {{ in_per_mtok: 3.0, out_per_mtok: 15.0 }} }}
    CHEAP:
      - {{ backend: anthropic, model_id: claude-haiku-4-5,
          api_key_env: ANTHROPIC_API_KEY,
          price: {{ in_per_mtok: 1.0, out_per_mtok: 5.0 }} }}
"""

_DEAD = "http://127.0.0.1:1/v1"  # reserved port, refuses instantly (test_heavy_tier_outage_e2e)


def _events(workspace: Path, event: str) -> list[dict[str, Any]]:
    rows = query(workspace, "SELECT payload FROM events WHERE event = ?", (event,))
    return [json.loads(raw) for (raw,) in rows]


def test_fixture_names_split_across_the_two_replicas() -> None:
    """The CLI arms' premise, measured rather than assumed: the two fixture repos are affine to
    DIFFERENT replicas, so arm (i) can show both endpoints serving and arm (iii) has a repo
    whose affine replica is the dead one."""
    assert sorted(replica_index(name, 2) for name in _NAMES) == [0, 1]


def test_fleet_scan_distributes_classify_calls_across_both_replicas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(i): both real endpoints serve, and the `llm_call` events record the per-endpoint split —
    one call per repo, on its affine replica."""
    with running_stub_server() as a, running_stub_server() as b:
        a.set_responder(_classify_responder)
        b.set_responder(_classify_responder)
        urls = (a.base_url, b.base_url)
        workspace = _make_workspace_custom(
            tmp_path, "split", models_yaml=_MODELS_YAML.format(a=urls[0], b=urls[1])
        )
        monkeypatch.chdir(workspace)
        result = scan_real_classify(workspace, json_output=True)
        assert result.exit_code == ExitCode.SUCCESS, result.output
        assert json.loads(result.stdout)["succeeded"] == len(_NAMES)
        assert len(a.requests) == 1 and len(b.requests) == 1, (a.requests, b.requests)

    served = sorted(call["base_url"] for call in _events(workspace, "llm_call"))
    assert served == sorted(urls), served
    expected = sorted(urls[replica_index(name, 2)] for name in _NAMES)
    assert served == expected


def test_fleet_scan_completes_on_the_live_replica_when_its_peer_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(iii): replica 1 refuses connections. The run still succeeds for BOTH repos — the one
    affine to the dead replica hops to the live one — the hop is a CONNECTION
    `backend_failover`, and no repo is charged for it (§12.43: no RHI, no failed repo)."""
    with pytest.raises(ConnectionRefusedError), socket.create_connection(("127.0.0.1", 1), 2):
        pass
    with running_stub_server() as live:
        live.set_responder(_classify_responder)
        workspace = _make_workspace_custom(
            tmp_path, "refuse", models_yaml=_MODELS_YAML.format(a=live.base_url, b=_DEAD)
        )
        monkeypatch.chdir(workspace)
        result = scan_real_classify(workspace, json_output=True)
        assert result.exit_code == ExitCode.SUCCESS, result.output
        payload = json.loads(result.stdout)
        assert payload["succeeded"] == len(_NAMES) and payload["failed"] == 0, payload
        assert len(live.requests) == len(_NAMES)

    assert {c["base_url"] for c in _events(workspace, "llm_call")} == {live.base_url}
    hops = _events(workspace, "backend_failover")
    assert [h["trigger"] for h in hops] == ["CONNECTION"], hops
    statuses = dict(query(workspace, "SELECT repo_id, status FROM phases WHERE phase = 1"))
    assert set(statuses.values()) == {"SUCCEEDED"}, statuses
    # not charged: the repo that hopped spent exactly the phase attempts its peer did. (`fleet
    # scan` writes no `attempts` rows — measured: the table is empty after this run — so the
    # `attempts.llm_failovers` count is asserted where it originates, `usage.llm_failovers`, in
    # the client-level (iii) test above; `workers/base.py`'s fold carries it to the column.)
    charged = dict(query(workspace, "SELECT repo_id, attempts FROM phases WHERE phase = 1"))
    assert len(set(charged.values())) == 1, charged
