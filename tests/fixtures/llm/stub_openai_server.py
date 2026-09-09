"""SPEC §12.41 piece 1: a real loopback OpenAI-compatible chat-completions stub server, plus a
network-origin assertion, as reusable test infrastructure.

**Why a real socket, not `httpx.MockTransport`.** Every test in
`tests/test_llm_backend_openai_compatible.py` drives a FAKE `ChatTransport` or a fake `httpx`
transport (`httpx.MockTransport`) — nothing there ever opens a socket, by that file's own
docstring. That is correct for testing the request builder and reply parser in isolation, but it
cannot prove the thing §12.41 actually needs proved: that `--profile local` reaches a REAL server
over a REAL loopback connection, with no external network reachable. This module is that missing
piece — a `ThreadingHTTPServer` bound to `127.0.0.1:0` (the OS assigns the port), speaking just
enough of the OpenAI chat-completions POST shape for `src/fleet/llm/backends/openai_compatible.py`
to round-trip against it for real.

**Why a background thread, not `asyncio.start_server`.** This test suite drives coroutines with
bare `asyncio.run(...)` per call (see `tests/test_llm_backend_openai_compatible.py`'s module
docstring and every `invoke()` helper in it) rather than pytest-asyncio fixtures or a
session-persistent event loop — there is no existing "shared event loop across a test" convention
to plug an `asyncio.start_server` into, and standing one up here would mean this fixture owns loop
lifecycle nobody else in the suite needs to reason about. A `ThreadingHTTPServer` on a daemon
thread needs no event loop at all: it starts before the async client code runs and stops after,
regardless of how many `asyncio.run()` calls happen in between. Stdlib only (`http.server`,
`socket`, `threading`) — no new dependency (CLAUDE.md Rule 2).

**Reply scripting mirrors `FakeTransport`'s own convention** (same file): `queue_reply(body)`
appends a body to a FIFO, popped one per request once more than one is queued (the last one
sticks, exactly like `FakeTransport._replies`); `set_responder(fn)` hands full control to a
callable when a canned body isn't enough (e.g. echoing back whatever schema the request declared).
Neither is required — a server with nothing scripted fails LOUD (CLAUDE.md Rule 11) rather than
silently answering with a made-up default, because a silently-wrong canned reply would be a worse
bug than a missing one: it would make an unscripted test pass by accident instead of failing where
the gap actually is.

**Reuse contract for a follow-up task driving the full pipeline (§12.41's second half):**
    from tests.fixtures.llm.stub_openai_server import running_stub_server, assert_loopback_only

    with running_stub_server() as server:
        server.queue_reply({...})  # or server.set_responder(callable)
        # point every profile target's base_url at server.base_url
        with assert_loopback_only():
            ...run the pipeline...
        assert server.requests  # what the pipeline actually sent, for post-hoc assertions

`server.base_url` already carries the `/v1` suffix `openai_compatible.py` expects
(`BackendTarget.base_url` gets `/chat/completions` appended by the OpenAI SDK itself). `.requests`
accumulates every decoded request body the server has seen, in order — handy for a test asserting
what a multi-repo, multi-role run actually dispatched.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: One decoded chat-completions request in, one response body out — or a `StatusReply` when the
#: test needs a non-200 status. `set_responder` is the escape hatch for a test that needs to
#: react to what was actually sent (e.g. reflect the requested `model`, or answer differently per
#: role) rather than a fixed queue of canned bodies.
Responder = Callable[[Mapping[str, object]], "Mapping[str, object] | StatusReply"]


class StubServerError(AssertionError):
    """Raised (and turned into a 500 for the client, never left to hang the connection) when the
    stub server receives a request it was not told how to answer. An `AssertionError` subclass,
    matching this project's Rule-11 "fail loud" stance rather than papering over a missing script
    with an invented default reply."""


class OffLoopbackConnectionAttempt(AssertionError):
    """Raised the instant `assert_loopback_only()`'s guard sees a `socket.connect()` whose target
    host is not loopback. §12.41's "no external network reachable" proof point, made mechanical."""


@dataclass(frozen=True)
class StatusReply:
    """A scripted reply carrying an explicit HTTP status code — round VI task 102's own addition,
    for §12.43 case (ii)'s real-CLI vehicle: a genuine `429` (which the `openai` SDK maps to
    `RateLimitError`, distinct from the 5xx/`APIStatusError` path a plain body cannot reach).
    `queue_reply`/`set_responder` still accept a bare `Mapping` for every existing caller — that
    continues to mean "200, this body", exactly as before this class existed; only a test that
    needs a NON-200 status wraps its body in one of these."""

    status: int
    body: Mapping[str, object]


# -------------------------------------------------------------------------------------------
# The server
# -------------------------------------------------------------------------------------------


class RecordingChatCompletionsServer:
    """A real `POST /v1/chat/completions` endpoint on `127.0.0.1:<ephemeral port>`.

    Every decoded request body is recorded in `.requests`, in arrival order — including one a
    follow-up test never scripted a reply for, which is what lets `StubServerError` name the
    request that had no answer. Reply scripting is thread-safe (`threading.Lock`) because
    `ThreadingHTTPServer` dispatches each connection on its own thread and a pipeline run may hold
    several concurrent calls open at once.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self._replies: list[Mapping[str, object]] = []
        self._responder: Responder | None = None
        self._lock = threading.Lock()
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> RecordingChatCompletionsServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        """`http://127.0.0.1:<port>/v1` — exactly the shape `config/models.yaml`'s `profiles.local`
        block already uses for its (currently placeholder) `base_url` entries."""
        port = self._httpd.server_address[1]
        return f"http://127.0.0.1:{port}/v1"

    def queue_reply(self, body: Mapping[str, object]) -> None:
        """FIFO, mirroring `FakeTransport._replies`
        (`tests/test_llm_backend_openai_compatible.py`): once more than one reply is queued, each
        request pops the next; the last one queued then answers every subsequent request."""
        with self._lock:
            self._replies.append(body)

    def set_responder(self, responder: Responder) -> None:
        """Overrides any queued replies for every request from this point on."""
        with self._lock:
            self._responder = responder
            self._replies = []

    def _answer(self, request_body: dict[str, object]) -> Mapping[str, object] | StatusReply:
        with self._lock:
            self.requests.append(request_body)
            if self._responder is not None:
                return self._responder(request_body)
            if not self._replies:
                raise StubServerError(
                    "the stub server received a chat-completions request with nothing scripted "
                    "to answer it — call queue_reply(...) or set_responder(...) before "
                    f"dispatching; the unanswered request was: {request_body!r}"
                )
            return self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]


def _decode_object(raw: bytes) -> dict[str, object]:
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise StubServerError(f"request body was not a JSON object: {raw!r}")
    return decoded


def _make_handler(server: RecordingChatCompletionsServer) -> type[BaseHTTPRequestHandler]:
    """A closure-bound handler class: `http.server` wants a class, not an instance, but the
    class needs to reach back into the ONE `RecordingChatCompletionsServer` that owns it."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format_: str, *args: object) -> None:
            pass  # keep pytest output free of one line per request

        def do_POST(self) -> None:  # http.server's own naming convention
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                decoded = _decode_object(raw)
                answer = server._answer(decoded)
                if isinstance(answer, StatusReply):
                    response_body, status = answer.body, answer.status
                else:
                    response_body, status = answer, 200
            except Exception as exc:  # the stub must answer, never hang the client
                response_body = {"error": {"message": str(exc)}}
                status = 500
            payload = json.dumps(response_body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


@contextmanager
def running_stub_server() -> Iterator[RecordingChatCompletionsServer]:
    """Start-stop lifecycle as a context manager — the primitive both the standalone proof test in
    this task and any pytest fixture wrapping it (module- or function-scoped, a follow-up task's
    call) can build on. Function-scoped is this task's own choice for the proof test below: a
    fresh server and a fresh ephemeral port per test means `.requests` never carries state across
    tests and two tests can never race for the same port — and starting a `ThreadingHTTPServer` on
    port 0 is a handful of syscalls, not a slow fixture, so there is no real cost to paying it
    once per test rather than sharing one server across a module.
    """
    server = RecordingChatCompletionsServer().start()
    try:
        yield server
    finally:
        server.stop()


# -------------------------------------------------------------------------------------------
# The network-origin assertion
# -------------------------------------------------------------------------------------------

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass
class LoopbackGuard:
    """What `assert_loopback_only()` yields. `blocked_attempts` is the mechanical proof a test
    reads: non-empty means the guard actually intercepted a real `socket.connect()` call, not
    merely that no violation happened to occur."""

    blocked_attempts: list[tuple[object, ...]] = field(default_factory=list)


@contextmanager
def assert_loopback_only() -> Iterator[LoopbackGuard]:
    """Monkeypatch `socket.socket.connect` for the duration of the block so ANY outbound TCP
    connection whose target host is not loopback raises `OffLoopbackConnectionAttempt` immediately
    — before the connection reaches the network — instead of silently going through.

    **Why the raw socket, not the `httpx`/`openai` transport layer.** The whole point of this
    fixture is that the server IS a real transport (a loopback socket) — swapping in a fake
    transport to "prove no network" would prove nothing new. `socket.socket.connect` is the one
    place both httpx's connection pool (sync or async — `asyncio`'s `loop.sock_connect` calls
    `sock.connect(address)` directly, per `Lib/asyncio/selector_events.py`) and the OpenAI SDK's
    own HTTP client ultimately arrive at, regardless of which of the two transports they are
    using. Patching one seam below both is simpler than patching two.

    A loopback connection is unaffected and passes straight through to the real `connect()`, so a
    test using this guard around a call to the fixture's own `running_stub_server()` should see
    `blocked_attempts == []` — that is the CONTROL half of Rule 12's discipline: the guard must
    stay silent on legitimate traffic, not just fire on illegitimate traffic.

    **D109 fix — Unix-domain sockets are never off-loopback.** `ProcessPoolExecutor`'s
    `forkserver` start method opens a control channel over an `AF_UNIX` socket, whose `connect()`
    target is a filesystem path **string** (verified directly: a throwaway script connecting a
    real `AF_UNIX` socket and printing what `socket.socket.connect` receives shows a plain `str`,
    e.g. `/tmp/pymp-.../listener-...`, never a `(host, port)` tuple). A Unix-domain socket cannot
    cross a machine boundary by construction — its "address" is a local filesystem path or the
    abstract namespace, not a network endpoint — so any `str` target is recognized as always safe
    here, one additional address SHAPE alongside the existing `(host, port)` tuple case. This does
    NOT touch the tuple branch below: a genuine off-loopback `AF_INET`/`AF_INET6` attempt (a tuple
    whose `host` is not in `_LOOPBACK_HOSTS`) still raises exactly as before.
    """
    guard = LoopbackGuard()
    real_connect = _socket_connect()

    def guarded_connect(sock: object, address: object) -> object:
        if isinstance(address, str):
            # AF_UNIX: a filesystem-path (or abstract-namespace) target never leaves the machine.
            return real_connect(sock, address)
        host = address[0] if isinstance(address, tuple) else address
        if host not in _LOOPBACK_HOSTS:
            guard.blocked_attempts.append((host, address))
            raise OffLoopbackConnectionAttempt(
                f"blocked an outbound connection attempt to {address!r} — only "
                f"{sorted(_LOOPBACK_HOSTS)} are permitted for the duration of this test"
            )
        return real_connect(sock, address)

    _set_socket_connect(guarded_connect)
    try:
        yield guard
    finally:
        _set_socket_connect(real_connect)


def _socket_connect() -> Callable[[object, object], object]:
    import socket

    return socket.socket.connect  # type: ignore[return-value]


def _set_socket_connect(fn: Callable[[object, object], object]) -> None:
    import socket

    socket.socket.connect = fn  # type: ignore[method-assign]
