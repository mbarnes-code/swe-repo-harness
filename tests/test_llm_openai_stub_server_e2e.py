"""SPEC §12.41 piece 1: proves the fixture in `tests/fixtures/llm/stub_openai_server.py` actually
works, standalone — before any follow-up task uses it to drive a real pipeline run.

Two things this file proves that `tests/test_llm_backend_openai_compatible.py` cannot, because
every test there drives a fake transport (that file's own module docstring says so):

1. A REAL socket, a REAL `AsyncOpenAI` client, a REAL `_SdkTransport` — nothing faked below
   `RecordingChatCompletionsServer` — round-trips through `openai_compatible.py`'s own client code
   (`LadderModelClient`, not a raw `httpx.get`) to a validated Pydantic value, negotiated at
   PROMPTED: the rung §12.41 requires a `CHEAP` target to reach, because this backend's UNDECLARED
   floor (`declared_capabilities()`, `openai_compatible.py`) is PROMPTED-only, matching the
   shipped `profiles.local` `CHEAP` entry's own deliberate omission of `capabilities_override`.
2. The network-origin assertion (`assert_loopback_only`) actually catches a genuine off-loopback
   attempt — CLAUDE.md Rule 12's discriminating-test discipline: proving the guard exists is not
   enough, it has to be shown firing on a real violation, not merely absent on a clean run.

This task does NOT close SPEC §12.41 — see this task's report
(`.superpowers/sdd/round-V-criteria-closure/task-21-report.md`) and its brief
(`task-21-brief.md`) for why: this is standalone fixture infrastructure, and a second task drives
the actual `scan → sequence → transform → build → verify → pr` chain under `--profile local`.
"""

from __future__ import annotations

import asyncio
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

import pytest
from pydantic import BaseModel

from fleet.llm.backends import openai_compatible as oc
from fleet.llm.client import LadderModelClient, Message, StructuredOutputMode, TierRoute
from fleet.models.enums import ModelTier
from fleet.models.tasks import BackendTarget
from tests.fixtures.llm.stub_openai_server import (
    OffLoopbackConnectionAttempt,
    assert_loopback_only,
    running_stub_server,
)


class Answer(BaseModel):
    verdict: str


class _SingleTargetRouter:
    """The same minimal `RoleRouter` stand-in `tests/test_llm_backend_openai_compatible.py` uses
    (`_SingleTargetRouter` there) — one target, resolved for every role."""

    def __init__(self, tgt: BackendTarget, tier: ModelTier = ModelTier.CHEAP) -> None:
        self._tgt = tgt
        self._tier = tier

    def resolve(self, role: str, *, tier_override: ModelTier | None = None) -> TierRoute:
        return TierRoute(tier=tier_override or self._tier, targets=(self._tgt,))


def _stub_target(base_url: str) -> BackendTarget:
    return BackendTarget.model_validate(
        {
            "backend": "openai_compatible",
            "model_id": "stub-model",
            "base_url": base_url,
            "price": "free",
        }
    )


def _square(x: int) -> int:
    """Module-level (picklable) worker for the `ProcessPoolExecutor` regression test below —
    `forkserver` sends the callable to the worker process by pickling it, which a closure or
    lambda cannot survive."""
    return x * x


def _chat_body(*, content: str, finish_reason: str = "stop") -> dict[str, object]:
    """A chat-completions body in the shape a real server returns — same fields
    `tests/test_llm_backend_openai_compatible.py`'s own `body()` helper builds, reproduced here
    rather than imported so this file has no dependency on that one's private helpers."""
    return {
        "model": "served-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


# -------------------------------------------------------------------------------------------
# 1. A real request, through the real client, to a real PROMPTED-floor validated value
# -------------------------------------------------------------------------------------------


def test_the_stub_server_answers_a_real_prompted_call_through_the_real_client() -> None:
    with running_stub_server() as server:
        server.queue_reply(_chat_body(content='{"verdict": "ok"}'))
        backend = oc.OpenAICompatibleBackend(oc._SdkTransport(max_retries=0), env={})
        client = LadderModelClient(
            _SingleTargetRouter(_stub_target(server.base_url)),
            {"openai_compatible": backend},
        )

        with assert_loopback_only() as guard:
            response = asyncio.run(
                client.complete("repo_classify", [Message(role="user", content="q")], Answer),
            )

        assert response.value.verdict == "ok"
        # §12.41 proof point 1: an `openai_compatible` target with no capability overrides — this
        # backend's undeclared floor — negotiates to PROMPTED and still yields a Pydantic-valid
        # value, exactly the property the shipped `local` profile's `CHEAP` tier relies on.
        assert response.mode is StructuredOutputMode.PROMPTED
        assert response.usage.backend == "openai_compatible"
        assert response.usage.cost_usd == 0.0  # `price: free`, declared
        assert len(server.requests) == 1
        sent = server.requests[0]
        assert sent["model"] == "stub-model"
        assert "response_format" not in sent and "tools" not in sent  # PROMPTED sends neither
        # The CONTROL half of Rule 12: a legitimate loopback call must not trip the guard.
        assert guard.blocked_attempts == []


def test_the_stub_server_records_every_request_it_answers() -> None:
    """`.requests` is what a follow-up task's pipeline-level assertions would read (e.g. `SELECT
    DISTINCT backend FROM llm_cache` has no equivalent for "what did the stub actually see" — this
    is that equivalent). Two calls, two recorded requests, in order."""
    with running_stub_server() as server:
        server.set_responder(lambda _req: _chat_body(content='{"verdict": "ok"}'))
        backend = oc.OpenAICompatibleBackend(oc._SdkTransport(max_retries=0), env={})
        client = LadderModelClient(
            _SingleTargetRouter(_stub_target(server.base_url)),
            {"openai_compatible": backend},
        )

        async def two_calls() -> None:
            await client.complete("repo_classify", [Message(role="user", content="one")], Answer)
            await client.complete("repo_classify", [Message(role="user", content="two")], Answer)

        with assert_loopback_only():
            asyncio.run(two_calls())

        assert len(server.requests) == 2


# -------------------------------------------------------------------------------------------
# 2. Rule 12 — the discriminating test: the guard has to actually catch a real violation
# -------------------------------------------------------------------------------------------


def test_the_loopback_guard_catches_a_genuine_off_loopback_attempt() -> None:
    """The real transport, pointed at a non-loopback LITERAL IP (`10.255.255.1` — a TEST-NET-3
    address, RFC 5737, never routable) so the failure is deterministic and needs no DNS lookup,
    no real network reachability, and no flakiness. If `assert_loopback_only()` were a no-op, this
    call would instead hang or fail with a `ConnectionRefused`/timeout from the SDK's own retry
    logic — NOT with `blocked_attempts` populated — so the assertion on `blocked_attempts` is what
    proves the GUARD fired, not merely that the call failed for some other reason."""
    backend = oc.OpenAICompatibleBackend(oc._SdkTransport(max_retries=0), env={})
    off_target = _stub_target("http://10.255.255.1:65535/v1")

    with assert_loopback_only() as guard, pytest.raises(Exception):  # noqa: B017
        asyncio.run(
            backend.invoke(
                off_target,
                [Message(role="user", content="hi")],
                None,
                StructuredOutputMode.PROMPTED,
                max_output_tokens=64,
                timeout_s=2.0,
            )
        )

    assert len(guard.blocked_attempts) >= 1, (
        "the guard never fired: the connection reached the socket layer unblocked"
    )
    blocked_host = guard.blocked_attempts[0][0]
    assert blocked_host == "10.255.255.1"


def test_the_guard_is_scoped_to_its_own_context_manager() -> None:
    """Restores the real `socket.socket.connect` on exit — a leaked patch would make every OTHER
    test in the suite silently subject to this guard, which is exactly the kind of cross-test
    contamination CLAUDE.md's scratch-isolation rules warn about for a different mechanism."""
    import socket

    real_connect = socket.socket.connect
    with assert_loopback_only():
        assert socket.socket.connect is not real_connect
    assert socket.socket.connect is real_connect


# -------------------------------------------------------------------------------------------
# 3. D109 regression — a Unix-domain socket (a real ProcessPoolExecutor's forkserver control
#    channel) must never trip the loopback guard, while a genuine off-loopback AF_INET attempt
#    still must.
# -------------------------------------------------------------------------------------------


def test_a_real_process_pool_executor_does_not_trip_the_loopback_guard() -> None:
    """D109: `ProcessPoolExecutor`'s `forkserver` start method opens its control channel over an
    `AF_UNIX` socket, whose `connect()` target is a filesystem-path `str`, not a `(host, port)`
    tuple. Before the fix, `assert_loopback_only()`'s tuple-only check misidentified that path as
    an off-loopback host and raised `OffLoopbackConnectionAttempt`, producing a false positive on
    any code path using a real process pool while the guard was engaged (see
    `docs/INTEGRATION_HONESTY.md` D109). `forkserver` is used explicitly here (rather than the
    platform default, `fork` on Linux) because `fork` never opens this control channel at all and
    so cannot reproduce the bug — verified by hand while diagnosing this defect: a `fork`-context
    pool leaves `blocked_attempts == []` even against the PRE-fix guard, `forkserver` does not.

    `guard.blocked_attempts == []` is the CONTROL half of Rule 12's discipline (same shape as
    `test_the_stub_server_answers_a_real_prompted_call_through_the_real_client` above): the guard
    must stay silent on this legitimate local IPC, not merely absent an off-loopback host.
    """
    ctx = multiprocessing.get_context("forkserver")
    with (
        assert_loopback_only() as guard,
        ProcessPoolExecutor(max_workers=2, mp_context=ctx) as pool,
    ):
        results = list(pool.map(_square, [1, 2, 3, 4]))
    assert results == [1, 4, 9, 16]
    assert guard.blocked_attempts == []


def test_the_loopback_guard_still_catches_a_genuine_violation_after_the_unix_socket_fix() -> None:
    """D109's regression proof: teaching the guard about `AF_UNIX` (a `str` address) must not
    weaken its `AF_INET`/`AF_INET6` (tuple address) check. Same deterministic TEST-NET-3 target
    and same assertions as `test_the_loopback_guard_catches_a_genuine_off_loopback_attempt` above
    (a real network socket, not the stub server, so it exercises the tuple branch directly rather
    than through the SDK's retry logic). A short `settimeout` guards against a hang if a future
    change to the guard ever lets this connection attempt reach the real network unblocked (a
    TEST-NET-3 address is never routable, so an unguarded attempt would otherwise stall on the
    connect() syscall rather than failing fast)."""
    import socket

    with assert_loopback_only() as guard:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        try:
            with pytest.raises(OffLoopbackConnectionAttempt):
                s.connect(("10.255.255.1", 65535))
        finally:
            s.close()

    assert len(guard.blocked_attempts) == 1
    assert guard.blocked_attempts[0][0] == "10.255.255.1"
