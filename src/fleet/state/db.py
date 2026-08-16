"""aiosqlite connection factory, PRAGMA discipline, and the single-writer `StateWriter` (§6, §11.5).

This module owns three things and nothing else:

1. **Connections.** `connect_ro()` opens the read-only (`mode=ro`) handle every worker gets;
   `writable_connection()` opens the one writable handle in the process. Both apply
   `PER_CONNECTION_PRAGMAS` on *every* handle, because those PRAGMAs do not persist — they reset
   to their SQLite defaults on each new connection. `foreign_keys` in particular defaults to
   **OFF**, and every `ON DELETE CASCADE` in `schema.sql` is silently inert without it, so a
   missed PRAGMA does not fail loudly: it leaks orphan rows for the life of the run.
   `journal_mode = WAL` and `user_version` are persistent, live in `schema.sql`, and are not
   re-issued here.

2. **The single-writer rule (§11.5).** Exactly one connection in the process performs writes and
   it belongs to the `StateWriter`. The slot is process-wide module state: while a `StateWriter`
   is live, any further attempt to open a writable connection raises
   `SingleWriterViolationError` rather than quietly becoming a second writer (§12 criterion 28).
   Workers never write SQL — they return a `WorkerResult` and the runner persists it.

3. **`StateWriter`.** One `asyncio.Queue` consumed by one task holding the one write connection.
   Every unit of work runs inside `BEGIN IMMEDIATE`, so the write lock is taken at statement
   start rather than at first write, which is what eliminates `SQLITE_BUSY` *mid*-transaction.

**THE WRITE-TRANSACTION CONTRACT — READ THIS BEFORE SUBMITTING A UNIT.**
A submitted unit is bounded to **pure SQL, <50 ms** (§6). **No network call, no git command, no
subprocess, and no LLM call may occur inside a write transaction.** This module cannot enforce
that mechanically — a `WriteUnit` is an arbitrary coroutine and nothing stops it from awaiting a
socket — so it is stated here as a contract the caller upholds. Violating it parks the single
write lock behind an unbounded external wait and stalls every other writer in the fleet; the
symptom is a run that appears to hang with no error at all. Compute *outside* the unit, pass the
finished values *in*, and let the unit do nothing but `INSERT`/`UPDATE`/`SELECT`.

`SQLITE_BUSY` / `SQLITE_BUSY_SNAPSHOT` is retried with jittered exponential backoff
(`MAX_BUSY_TRIES` = 8) and is classified `BUSY_FAILURE_CLASS` — `FailureClass.TRANSIENT_INFRA`,
which **never increments `phases.attempts`** (§6, §11.8). A lock contention is an infrastructure
event, not a failed attempt by a repo.

Errors are never swallowed: an exception raised inside a unit is rolled back and re-raised **to
the caller that submitted it**, and the actor stays up for the next submit (CLAUDE.md Rule 11).

DDL is **not** applied here implicitly. `initialize_database()` is the callable
`fleet migrate-db` (§10) uses to bring a *fresh* database to the v7 baseline, and nothing on the
worker/runner startup path may call it — §6's migration policy is normative: a startup that
"applies `schema.sql` idempotently" leaves an old database at its old shape, because
`CREATE TABLE IF NOT EXISTS` never executes an `ALTER` or a rebuild.

`state/fleet.db` MUST live on a local filesystem: WAL needs shared memory, which an NFS/SMB mount
does not provide, and the file corrupts rather than degrades (§6).
"""

from __future__ import annotations

import asyncio
import random
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Final

import aiosqlite

from fleet.models.enums import FailureClass
from fleet.models.state import SCHEMA_VERSION

__all__ = [
    "BUSY_FAILURE_CLASS",
    "DEFAULT_BUSY_TIMEOUT_MS",
    "DEFAULT_DB_PATH",
    "MAX_BUSY_TRIES",
    "PER_CONNECTION_PRAGMAS",
    "SCHEMA_PATH",
    "SingleWriterViolationError",
    "StateDbError",
    "StateWriteBusyError",
    "StateWriter",
    "StateWriterClosedError",
    "StateWriterNotStartedError",
    "WriteUnit",
    "connect_ro",
    "initialize_database",
    "read_user_version",
    "writable_connection",
    "write_slot_owner",
]

DEFAULT_DB_PATH: Final = Path("state/fleet.db")
SCHEMA_PATH: Final = Path(__file__).with_name("schema.sql")

#: Every PRAGMA that resets on a new handle. `journal_mode`/`user_version` are persistent and
#: live in `schema.sql`; issuing them here would be redundant on read handles and wrong on none.
PER_CONNECTION_PRAGMAS: Final[tuple[str, ...]] = (
    "PRAGMA foreign_keys = ON",  # OFF by default: every CASCADE in schema.sql is inert without it
    "PRAGMA busy_timeout = {busy_timeout_ms}",  # backstop only, never the arbitrator (ADR-0004)
    "PRAGMA synchronous = NORMAL",  # the correct pairing with WAL
    "PRAGMA wal_autocheckpoint = 1000",
)

DEFAULT_BUSY_TIMEOUT_MS: Final = 30_000
MAX_BUSY_TRIES: Final = 8
_BACKOFF_BASE_S: Final = 0.05
_BACKOFF_CAP_S: Final = 2.0

#: A busy retry is TRANSIENT_INFRA and is NEVER counted against `phases.attempts` (§6, §11.8).
BUSY_FAILURE_CLASS: Final = FailureClass.TRANSIENT_INFRA

_SQLITE_BUSY_PRIMARY: Final = 5  # SQLITE_BUSY; SQLITE_BUSY_SNAPSHOT/RECOVERY/TIMEOUT extend it

type WriteUnit[T] = Callable[[aiosqlite.Connection], Awaitable[T]]
"""A unit of work run inside one `BEGIN IMMEDIATE`. Pure SQL only — see the module docstring."""


class StateDbError(RuntimeError):
    """Base class for every failure this module raises. Never caught-and-ignored internally."""


class SingleWriterViolationError(StateDbError):
    """A second writable connection was requested while one is already held (§11.5)."""


class StateWriterClosedError(StateDbError):
    """Submitted to a writer that is closed or closing. Raised, never parked on a dead queue."""


class StateWriterNotStartedError(StateDbError):
    """Submitted before `start()`. Fails loudly instead of queueing into a task that never runs."""


class StateWriteBusyError(StateDbError):
    """`MAX_BUSY_TRIES` exhausted against `SQLITE_BUSY`. Classified `BUSY_FAILURE_CLASS`."""

    failure_class: Final = BUSY_FAILURE_CLASS


class SchemaVersionError(StateDbError):
    """`PRAGMA user_version` is not the version compiled into the harness (§6)."""


# --------------------------------------------------------------------------------------
# the single-writer slot — process-wide, because the rule is process-wide
# --------------------------------------------------------------------------------------

_write_slot_owner: str | None = None


def write_slot_owner() -> str | None:
    """Who holds the process's one writable connection, or `None`. Diagnostics and tests."""
    return _write_slot_owner


def _acquire_write_slot(owner: str) -> None:
    global _write_slot_owner
    if _write_slot_owner is not None:
        raise SingleWriterViolationError(
            f"a writable connection is already held by {_write_slot_owner!r}; "
            f"{owner!r} may not open a second one (SPEC §11.5 single-writer rule). "
            "Workers read through connect_ro() and submit writes to the StateWriter."
        )
    _write_slot_owner = owner


def _release_write_slot() -> None:
    global _write_slot_owner
    _write_slot_owner = None


# --------------------------------------------------------------------------------------
# connections
# --------------------------------------------------------------------------------------


async def _apply_per_connection_pragmas(
    conn: aiosqlite.Connection, *, busy_timeout_ms: int
) -> None:
    for statement in PER_CONNECTION_PRAGMAS:
        await conn.execute(statement.format(busy_timeout_ms=busy_timeout_ms))


def _read_only_uri(path: Path) -> str:
    """Sync on purpose: `Path.resolve()` touches the filesystem and must not run in a coroutine."""
    return f"file:{path.resolve().as_posix()}?mode=ro"


async def connect_ro(
    path: Path = DEFAULT_DB_PATH, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
) -> aiosqlite.Connection:
    """Open a read-only (`mode=ro`) connection. This is the handle every worker gets.

    Read-only is not advisory: an `INSERT` on this connection raises `SQLITE_READONLY`, which is
    the mechanism that stops a worker from becoming a second writer. Under WAL these readers run
    concurrently with the writer (§11.5). The caller closes it.
    """
    uri = _read_only_uri(path)
    conn = await aiosqlite.connect(uri, uri=True, isolation_level=None)
    try:
        await _apply_per_connection_pragmas(conn, busy_timeout_ms=busy_timeout_ms)
    except BaseException:
        await conn.close()
        raise
    return conn


@asynccontextmanager
async def writable_connection(
    path: Path = DEFAULT_DB_PATH,
    *,
    owner: str,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> AsyncIterator[aiosqlite.Connection]:
    """The ONLY way to obtain a writable handle. Raises if the process already holds one.

    `owner` names the holder so the violation message says who is fighting whom.
    `isolation_level=None` puts the driver in autocommit mode so transactions are exactly the
    ones this module issues — `BEGIN IMMEDIATE` — and never an implicit one the driver inserted.
    """
    _acquire_write_slot(owner)
    conn: aiosqlite.Connection | None = None
    try:
        conn = await aiosqlite.connect(path, isolation_level=None)
        await _apply_per_connection_pragmas(conn, busy_timeout_ms=busy_timeout_ms)
        yield conn
    finally:
        if conn is not None:
            await conn.close()
        _release_write_slot()


async def read_user_version(conn: aiosqlite.Connection) -> int:
    """`PRAGMA user_version`. Workers read this at startup and refuse to start on a mismatch."""
    async with conn.execute("PRAGMA user_version") as cursor:
        row = await cursor.fetchone()
    if row is None:  # pragma: no cover — PRAGMA user_version always returns a row
        raise StateDbError("PRAGMA user_version returned no row")
    return int(row[0])


async def initialize_database(
    path: Path = DEFAULT_DB_PATH,
    *,
    schema_path: Path = SCHEMA_PATH,
) -> int:
    """Apply `schema.sql` to a database and return its `PRAGMA user_version`.

    **This is the callable `fleet migrate-db` (§10) uses, and the only caller it may have.** It
    MUST NOT run implicitly on worker or runner startup: §6's migration policy is normative —
    `CREATE TABLE IF NOT EXISTS` creates a *missing* schema and never executes an `ALTER` or a
    rebuild, so an implicit "apply idempotently" leaves an existing database at its old shape
    while reporting success. Fresh databases land directly at `SCHEMA_VERSION`; databases that
    already hold data go through the ordered `migrations/vNNN_*.py` ladder instead.

    It takes the single-writer slot for its duration, so it also cannot run behind a live
    `StateWriter`. `foreign_keys` stays ON here because this file is pure `CREATE`; the
    table-rebuild migrations own their own `foreign_keys = OFF` window (§6).
    """
    schema_sql = await asyncio.to_thread(schema_path.read_text, encoding="utf-8")
    await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
    async with writable_connection(path, owner="migrate-db") as conn:
        await conn.executescript(schema_sql)
        version = await read_user_version(conn)
    if version != SCHEMA_VERSION:
        raise SchemaVersionError(
            f"{schema_path} left user_version={version}, expected {SCHEMA_VERSION}"
        )
    return version


# --------------------------------------------------------------------------------------
# the single-writer actor
# --------------------------------------------------------------------------------------


def _is_busy(exc: sqlite3.Error) -> bool:
    """True for SQLITE_BUSY and its extended forms (BUSY_SNAPSHOT/RECOVERY/TIMEOUT)."""
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        return code & 0xFF == _SQLITE_BUSY_PRIMARY
    return False


def _backoff_delay(attempt: int) -> float:
    """Full-jitter exponential backoff: uniform(0, min(cap, base * 2**(attempt-1)))."""
    ceiling = min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * 2.0 ** (attempt - 1))
    return random.uniform(0.0, ceiling)  # noqa: S311 — backoff jitter, not a security decision


@dataclass(slots=True)
class _WriteRequest:
    unit: WriteUnit[Any]
    future: asyncio.Future[Any]


class StateWriter:
    """The one write path. One queue, one task, one connection, every unit `BEGIN IMMEDIATE`.

    Usage — prefer the context manager, which cannot leak the write slot::

        async with StateWriter(db_path) as writer:
            seq = await writer.submit(insert_event)

    `submit()` returns whatever the unit returned, to *that* caller; an exception inside the unit
    is rolled back and re-raised to *that* caller and does not disturb the actor or any other
    submitter. See the module docstring for the write-transaction contract (pure SQL, no network,
    no git, no LLM) — it is the caller's to uphold.
    """

    def __init__(
        self,
        path: Path = DEFAULT_DB_PATH,
        *,
        owner: str = "StateWriter",
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        max_busy_tries: int = MAX_BUSY_TRIES,
    ) -> None:
        self._path = path
        self._owner = owner
        self._busy_timeout_ms = busy_timeout_ms
        self._max_busy_tries = max_busy_tries
        self._queue: asyncio.Queue[_WriteRequest | None] = asyncio.Queue()
        self._conn: aiosqlite.Connection | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._holds_slot = False
        self.busy_retries = 0
        """Count of SQLITE_BUSY encounters retried. TRANSIENT — never a task attempt (§11.8)."""

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._closed

    async def start(self) -> None:
        """Take the process's write slot, open the one write connection, run the pump task."""
        if self._closed:
            raise StateWriterClosedError(f"{self._owner} is closed and cannot be restarted")
        if self._task is not None:
            raise StateDbError(f"{self._owner} is already started")
        _acquire_write_slot(self._owner)
        self._holds_slot = True
        try:
            self._conn = await aiosqlite.connect(self._path, isolation_level=None)
            await _apply_per_connection_pragmas(self._conn, busy_timeout_ms=self._busy_timeout_ms)
        except BaseException:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None
            self._holds_slot = False
            _release_write_slot()
            raise
        self._task = asyncio.create_task(self._pump(), name=f"{self._owner}-pump")

    async def aclose(self) -> None:
        """Drain the queue, stop the pump, close the connection, release the write slot.

        Idempotent. Work already submitted is completed first (the shutdown sentinel is FIFO
        behind it); work submitted after this returns raises `StateWriterClosedError`.
        """
        if self._closed and self._task is None:
            return
        self._closed = True
        try:
            if self._task is not None:
                await self._queue.put(None)
                await self._task
                self._task = None
        finally:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None
            if self._holds_slot:
                self._holds_slot = False
                _release_write_slot()

    async def __aenter__(self) -> StateWriter:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def submit[T](self, unit: WriteUnit[T]) -> T:
        """Queue `unit` and await its result. Pure SQL only — see the module docstring."""
        if self._closed:
            raise StateWriterClosedError(
                f"{self._owner} is closed; this write was never queued (SPEC §11.5)"
            )
        if self._task is None:
            raise StateWriterNotStartedError(f"{self._owner}.start() has not been called")
        future: asyncio.Future[T] = asyncio.get_running_loop().create_future()
        await self._queue.put(_WriteRequest(unit=unit, future=future))
        return await future

    # -- internals ----------------------------------------------------------------------

    async def _pump(self) -> None:
        try:
            while True:
                request = await self._queue.get()
                try:
                    if request is None:
                        return
                    await self._serve(request)
                finally:
                    self._queue.task_done()
        finally:
            self._fail_pending()

    async def _serve(self, request: _WriteRequest) -> None:
        """Run one unit and hand the outcome — result OR exception — back to its submitter."""
        try:
            result = await self._run_with_busy_retry(request.unit)
        except Exception as exc:  # the caller's error is the caller's to see, verbatim
            if not request.future.done():
                request.future.set_exception(exc)
        else:
            if not request.future.done():
                request.future.set_result(result)

    async def _run_with_busy_retry[T](self, unit: WriteUnit[T]) -> T:
        conn = self._conn
        if conn is None:  # pragma: no cover — the pump only runs with a live connection
            raise StateWriterNotStartedError(f"{self._owner} has no connection")
        last: sqlite3.Error | None = None
        for attempt in range(1, self._max_busy_tries + 1):
            try:
                return await _run_in_immediate(conn, unit)
            except sqlite3.Error as exc:
                if not _is_busy(exc):
                    raise
                last = exc
                self.busy_retries += 1
                if attempt < self._max_busy_tries:
                    await asyncio.sleep(_backoff_delay(attempt))
        raise StateWriteBusyError(
            f"SQLITE_BUSY after {self._max_busy_tries} tries on {self._path}; "
            f"classified {BUSY_FAILURE_CLASS.value} — this is NOT a task attempt (§11.8)"
        ) from last

    def _fail_pending(self) -> None:
        """Nobody waits forever on a dead actor: every parked future gets the closed error."""
        while True:
            try:
                request = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if request is not None and not request.future.done():
                request.future.set_exception(
                    StateWriterClosedError(f"{self._owner} shut down before this write ran")
                )


async def _run_in_immediate[T](conn: aiosqlite.Connection, unit: WriteUnit[T]) -> T:
    """`BEGIN IMMEDIATE` → unit → `COMMIT`, or roll back and re-raise. Lock at statement start."""
    await conn.execute("BEGIN IMMEDIATE")
    try:
        result = await unit(conn)
    except BaseException:
        await conn.rollback()
        raise
    await conn.commit()
    return result
