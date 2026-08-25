"""§11.5 step 6's SQL surface, guarded by a WHITELIST of what the write may touch AT ALL.

**Why this file exists.** `da45a43` proved *"`append_unblocked_wave` touches no existing `waves`
row"* **per call**, by hand, in a lane's scratch script — two instruments (a full value dump of
every user table, and an `AFTER INSERT/UPDATE/DELETE` trigger log) enumerating what WAS written.
That proof was never carried into the suite, so nothing here stopped a later edit from writing
somewhere new. `1d0c8f6` then wired the step behind a CLI command, which widened the surface:
the write is now `SqliteStateRepository.clear_blocked_by` **plus** `append_unblocked_wave`, run
back to back from `cli._unblock_dependents`.

**Why a whitelist and not a list of forbidden sinks.** A blacklist only forbids the forms someone
predicted; the third escape always defeats it. `STEP6_SQL_TARGETS` instead names every
`(verb, table)` pair step 6 may execute, so a write has to *be added to the set* before it can
pass — the shape `TRANSITION_GLOBALS` (`tests/test_state_models.py`) and
`UNAVAILABLE_MESSAGE_VOCABULARY` already use in this tree. The verb set is a whitelist too: a
statement whose leading keyword is neither a known write verb nor a known read/transaction verb
is reported as **unresolved** and fails, rather than being silently classified as harmless.

**The quantity watched, and what could leave it unchanged.** The instrument watches *the set of
`(verb, table)` pairs SQLite executed, on any connection, while `cli._unblock_dependents` was on
the stack* — captured with `sqlite3.Connection.set_trace_callback`, which fires on statement
execution and is therefore indifferent to whether a statement changed a row. That is the
complement of the scratch proof's value dump, which is blind to a value-preserving write
(`UPDATE waves SET synthetic = synthetic` moves no value and dumps identically): here that
statement is the pair `("UPDATE", "waves")`, which is **not** in the whitelist, so it fails. The
measurement is in `.superpowers/sdd/round-e/lanes/W28/report.md`.

**Disclosed residue — this is a boundary, not closure.**

* A side effect routed only through *whitelisted* pairs passes: a second `UPDATE phases` that
  cleared an unrelated column is a pair the set already contains. `test_step_6_moves_exactly_one
  _member_and_leaves_every_existing_wave_row_alone` narrows that for `waves`/`wave_members` by
  comparing rows, and `tests/test_resume_unblocking.py` pins the `phases` rows; neither this file
  nor that one bounds *which* `phases` rows a whitelisted `UPDATE phases` may touch.
* Reads are **not** constrained. Only write verbs enter the whitelist, so a new `SELECT` against
  any table passes silently — deliberately, so that a read added for a diagnostic does not redden
  the suite.
* A write issued on a connection SQLite's tracer cannot see (a subprocess, another driver) is
  invisible. Every write in this path goes through the run's single `StateWriter` (§11.5); that
  is a property of the harness, not something this file measures.
* No length, count or size bound is used anywhere below, so there is no threshold that can rot
  into a silent pass as the statements grow.
* **Three `write_target` boundaries review lane CR8 named, disposed here rather than left
  undisclosed.** (i) A **schema-qualified** table resolves to its table half — `_resolve_table`
  follows `schema.table` to `table`, so `INSERT INTO <attached>.waves …` resolves to the
  whitelisted `('INSERT', 'waves')`. It needs an `ATTACH` first, which makes it adversarial-only:
  disclosed, deliberately not patched, per CLAUDE.md's stop rule. (ii) A **multi-statement**
  string whose leading verb is a read — `"SELECT 1; UPDATE waves SET max_usd = 1"` → `None`. This
  one is **unreachable**, measured rather than argued: on CPython 3.12.3
  `sqlite3.Connection.execute` raises `ProgrammingError: You can only execute one statement at a
  time`, and `executescript` reaches the trace callback **already split** (`'SELECT 1;'` and
  `' UPDATE t SET a=1;'` arrive as two separate callbacks). It is recorded as unreachable, and
  with the reason, because an over-disclosed boundary misleads a reader the same way an
  undisclosed one does. (iii) `ANALYZE` was classified as a non-write and writes `sqlite_stat1` —
  that one is **fixed**, not disclosed; see `_NON_WRITE_VERBS`. The same shape one keyword over,
  `PRAGMA`, is fixed too: see `_NON_WRITE_PRAGMAS`.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import aiosqlite
import pytest

from fleet import cli
from fleet.models.enums import Phase
from fleet.orchestrator.scheduler import SqliteSchedulerStore, WaveScheduler, WaveState
from fleet.settings import BudgetsSection
from fleet.state.db import PER_CONNECTION_PRAGMAS, StateWriter, connect_ro
from fleet.state.repository import SqliteStateRepository
from tests.test_cli import RUN_ID
from tests.test_resume_unblocking import (
    FREED,
    _membership,
    _resume,
    fleet,  # noqa: F401  — the fixture this file drives; see the module docstring
)

STEP6_SQL_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("UPDATE", "phases"),
        ("INSERT", "waves"),
        ("UPDATE", "wave_members"),
    }
)
"""Every `(verb, table)` pair §11.5 step 6 may execute. Captured, not predicted.

* `("UPDATE", "phases")` — `SqliteStateRepository.clear_blocked_by`, one row per `phases` row
  that actually carries a removed name.
* `("INSERT", "waves")` — `SchedulerStore.append_unblocked_wave`'s appended synthetic wave.
* `("UPDATE", "wave_members")` — the same method moving the freed members up into it.

A pair absent from this set fails the run **naming the pair**; a pair in the set that the run did
not execute fails it too, because a step 6 that stopped moving members is the known-bad state
this instrument was validated against.
"""

_NON_WRITE_VERBS: Final[frozenset[str]] = frozenset(
    {
        "SELECT", "BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE",
        "EXPLAIN", "ATTACH", "DETACH",
    }
)
"""Leading keywords that execute no row write. Anything outside this set and outside the
resolvers below is reported as unresolved rather than assumed harmless — `VACUUM`, `REINDEX`,
`ANALYZE` and a CTE-prefixed `WITH … INSERT` all land there on purpose.

`PRAGMA` was **in** this set until this lane measured that a leading `PRAGMA` decides nothing on
its own — `PRAGMA foreign_keys = ON` writes nothing and `PRAGMA user_version = 9` writes the
database header. It is resolved by argument now; see `_NON_WRITE_PRAGMAS`.

`ANALYZE` was **in** this set until review lane CR8 measured that it is not a non-write: on
CPython 3.12.3's SQLite it creates `sqlite_stat1` and writes a row into it (measured here:
`sqlite_master` gains `sqlite_stat1`, which then holds `('t', 'i', '50 1')`). Nothing in step 6
runs it and `sqlite_stat1` is outside `SNAPSHOT_TABLES`, so the misclassification costs no
coverage today — it is corrected because this is a set asserted by **equality**, and the one
place a misclassification widens the permitted surface silently instead of narrowing it is the
non-write half. Removing it does not add a pair to the observed set: `ANALYZE` now falls to
`write_target`'s final `else` and would be reported as `("ANALYZE", "?")`, loud."""

_UNRESOLVED: Final = "?"
_DDL_NOISE: Final[frozenset[str]] = frozenset(
    {"TEMP", "TEMPORARY", "TABLE", "INDEX", "VIEW", "TRIGGER", "VIRTUAL", "UNIQUE", "IF", "NOT",
     "EXISTS"}
)

_TOKEN: Final = re.compile(
    r"'(?:[^']|'')*'"        # string literal (the tracer expands bound parameters into these)
    r'|"(?:[^"]|"")*"'       # quoted identifier
    r"|\[[^\]]*\]"           # bracketed identifier
    r"|`(?:[^`]|``)*`"       # backticked identifier
    r"|--[^\n]*"             # line comment
    r"|/\*.*?\*/"            # block comment
    r"|[A-Za-z_][A-Za-z_0-9$]*"
    r"|\S",
    re.DOTALL,
)
"""A token scanner rather than a statement-shaped regex, and **unbounded** by construction.

Quotes and comments are matched *before* bare words, so a keyword inside an expanded parameter
cannot be read as a keyword, and reflowing or re-indenting a SQL literal cannot change any token
(the control check in this lane's report measures that). There is no `{0,N}` repetition anywhere:
a bound would silently stop matching once a statement grew past it.
"""

_IDENTIFIER: Final = re.compile(r"^[A-Za-z_][A-Za-z_0-9$]*$")


def _tokens(sql: str) -> list[str]:
    return [
        tok for tok in _TOKEN.findall(sql)
        if not tok.startswith("--") and not tok.startswith("/*")
    ]


def _bare(token: str) -> str | None:
    """A table name with its quoting stripped, or `None` if the token is not a bare identifier."""
    stripped = token.strip('"`')
    if stripped.startswith("[") and stripped.endswith("]"):
        stripped = stripped[1:-1]
    return stripped.lower() if _IDENTIFIER.match(stripped) else None


def _resolve_table(rest: Sequence[str]) -> str | None:
    """The table a write names, following a `schema.table` qualifier to the table half.

    A `PRAGMA`'s name has the same `[schema.]name` grammar (`PRAGMA main.user_version = 9`), so
    `write_target`'s `PRAGMA` branch resolves through here too.
    """
    if not rest:
        return None
    if len(rest) >= 3 and rest[1] == ".":
        return _bare(rest[2])
    return _bare(rest[0])


_NON_WRITE_PRAGMAS: Final[frozenset[str]] = frozenset(
    {"foreign_keys", "busy_timeout", "synchronous", "wal_autocheckpoint"}
)
"""PRAGMA names that write nothing. A `PRAGMA` outside this set is `("PRAGMA", "?")` — loud.

**Why the verb alone cannot decide.** `PRAGMA` was in `_NON_WRITE_VERBS`, which exempted every
form of it. Measured on this tree's interpreter (CPython 3.12.3 / SQLite 3.45.1) by hashing the
database file and re-reading `sqlite_master` around each statement on a fresh 50-row database:
`PRAGMA journal_mode = WAL`, `PRAGMA user_version = 99` and `PRAGMA application_id = 7` all
change the file; the four names above, and the read forms `PRAGMA user_version`,
`PRAGMA foreign_key_check`, `PRAGMA table_info("t")`, do not. So the leading keyword carries no
information about whether a row or a header was written and the argument has to be read.

**Why the set is these four names and not a shape rule.** "A `PRAGMA` with no `=` is a query, and
a query is a read" is false, measured: `PRAGMA optimize` — no `=`, no argument — creates
`sqlite_stat1` and writes `('t', 'i', '50 1')` into it, once any query against an indexed column
has run on the same connection (with no such query first it writes nothing; both arms measured).
These four are the names `PER_CONNECTION_PRAGMAS` issues on every handle the run opens, and they
are here as a **literal** rather than derived from that tuple on purpose: deriving the exemption
from the same declaration that produces the statements would make a wrong addition to the tuple
exempt itself. `test_every_pragma_the_connection_factory_issues_is_classified` cross-checks the
two, so production cannot grow **`PER_CONNECTION_PRAGMAS`** without this set being edited. It does
*not* reach a per-connection PRAGMA added outside that tuple, and the earlier wording here
("cannot grow a per-connection PRAGMA") claimed that it did. Measured (round F, lane W9,
zero-change gate `1 0` read before the result): `await conn.execute("PRAGMA journal_mode = WAL")`
appended to `_apply_per_connection_pragmas` leaves that cross-check **green**, and
`test_step_6_executes_only_the_whitelisted_verb_table_pairs` and
`test_step_6_moves_exactly_one_member_and_leaves_every_existing_wave_row_alone` are what fail
(2 failed, 21 passed). The two instruments are complementary in both directions: that one fires on
a pragma added to a code path step 6 never reaches, these two on a pragma issued anywhere inside
the window.

**Disclosed cost.** Classification is by name, and `PRAGMA user_version` (a read, used by
`fleet.state.db.read_user_version`) and `PRAGMA user_version = 9` (a header write) share one
name. The writing form is what decides, so the read form is loud too if it ever enters the
window. That is the whitelist's standard trade and it costs nothing today: step 6 executes 12
PRAGMA statements and all 12 are of the four names above."""


def write_target(sql: str) -> tuple[str, str] | None:
    """`(VERB, table)` for a statement that writes rows, else `None` for a read or a transaction.

    A write whose table cannot be resolved returns `(VERB, "?")` — loud, and it fails the
    assertion naming the statement. Failing on an unresolvable form is the honest behaviour for a
    whitelist: the alternative is a recognition step that drops what it cannot parse, which is
    how a detector goes silent on exactly the site it exists to catch.

    A statement that tokenises to **nothing while its text is non-empty** is that same recognition
    failure and is reported as `("?", "?")` rather than as a read. It is not hypothetical: SQLite's
    trace callback renders an internally generated nested statement as a `--`-prefixed single line,
    `_TOKEN` matches that whole line as a comment, and every token is then dropped. Measured on
    CPython 3.12.3: `PRAGMA optimize` traces as `PRAGMA optimize`, then `-- ANALYZE "main"."t"`,
    then a `--`-prefixed `SELECT` — so the one statement of the three that writes `sqlite_stat1`
    was, before this branch, the one classified as harmless. Both `--`-prefixed lines resolve to
    `("?", "?")` and the leading `PRAGMA optimize` to `("PRAGMA", "?")` (`_NON_WRITE_PRAGMAS`), so
    all three of them are loud now, by two independent branches. Measured cost of the branch on the
    real capture: step 6 executes **37 statements / 25 distinct forms** inside the window and
    **0** of them are comment-only, so this adds no pair to the observed set.
    """
    tokens = _tokens(sql)
    if not tokens:
        return None if not sql.strip() else (_UNRESOLVED, _UNRESOLVED)
    verb = tokens[0].upper()
    rest = tokens[1:]
    if verb in _NON_WRITE_VERBS:
        return None
    if verb == "PRAGMA":
        return None if _resolve_table(rest) in _NON_WRITE_PRAGMAS else (verb, _UNRESOLVED)
    if verb in {"INSERT", "REPLACE"}:
        if rest and rest[0].upper() == "OR":
            rest = rest[2:]
        if not rest or rest[0].upper() != "INTO":
            return (verb, _UNRESOLVED)
        rest = rest[1:]
    elif verb == "UPDATE":
        if rest and rest[0].upper() == "OR":
            rest = rest[2:]
    elif verb == "DELETE":
        if not rest or rest[0].upper() != "FROM":
            return (verb, _UNRESOLVED)
        rest = rest[1:]
    elif verb in {"CREATE", "DROP", "ALTER"}:
        while rest and rest[0].upper() in _DDL_NOISE:
            rest = rest[1:]
    else:
        return (verb, _UNRESOLVED)
    return (verb, _resolve_table(rest) or _UNRESOLVED)


class SqlTrace:
    """Every statement SQLite executed while step 6 was on the stack.

    The callback runs in aiosqlite's worker thread; `record` only appends, and the window is
    opened and closed around an `await`ed call, so no statement of step 6's can land outside it.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.recording = False

    def record(self, statement: str) -> None:
        if self.recording:
            self.statements.append(statement)

    def writes(self) -> dict[tuple[str, str], str]:
        """The observed write pairs, each mapped to one statement that produced it."""
        seen: dict[tuple[str, str], str] = {}
        for sql in self.statements:
            target = write_target(sql)
            if target is not None:
                seen.setdefault(target, " ".join(sql.split()))
        return seen


@pytest.fixture
def step6_sql(monkeypatch: pytest.MonkeyPatch) -> Iterator[SqlTrace]:
    """Trace every connection the process opens; record only inside `_unblock_dependents`.

    Both halves matter. Tracing at `aiosqlite.connect` means a connection step 6 inherits from an
    earlier step is traced too, so the window — not the connection — is what scopes the capture.
    """
    trace = SqlTrace()
    real_connect = aiosqlite.connect
    real_step6 = cli._unblock_dependents

    async def connect(*args: Any, **kwargs: Any) -> aiosqlite.Connection:
        conn = await real_connect(*args, **kwargs)
        await conn.set_trace_callback(trace.record)
        return conn

    async def windowed(*args: Any, **kwargs: Any) -> dict[str, object]:
        trace.recording = True
        try:
            return await real_step6(*args, **kwargs)
        finally:
            trace.recording = False

    monkeypatch.setattr(aiosqlite, "connect", connect)
    monkeypatch.setattr(cli, "_unblock_dependents", windowed)
    yield trace


SNAPSHOT_TABLES: Final[tuple[str, ...]] = ("phases", "waves", "wave_members")
"""The tables `STEP6_SQL_TARGETS` names. Interpolated into `_dump`'s SQL, so it is a literal
tuple in this module and never a value from the database or the environment."""


def _dump(db: Path, table: str) -> list[tuple[object, ...]]:
    """Every column of every row, so an added row and a changed value are both visible."""
    assert table in SNAPSHOT_TABLES, f"{table!r} is not one of this module's own table names"
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(f"SELECT * FROM {table}")  # noqa: S608 - asserted literal above
        return sorted((tuple(row) for row in rows), key=repr)
    finally:
        conn.close()


def _wave_states(db: Path) -> dict[int, WaveState]:
    """`WaveScheduler.wave_state` per wave — the PRODUCTION computation, not a re-derivation."""

    def clock() -> datetime:
        """Fixed: every wave in the fixture is either settled or has a NULL `wave_started_at`,
        so `breached` is never the deciding term and a moving clock would add nothing."""
        return datetime(2026, 8, 9, 12, 0, tzinfo=UTC)

    async def compute() -> dict[int, WaveState]:
        async with StateWriter(db, owner="test:w28:wave-state") as writer:
            read_conn = await connect_ro(db)
            try:
                store = SqliteSchedulerStore(writer=writer, read_conn=read_conn)
                repository = SqliteStateRepository(writer=writer, read_conn=read_conn)
                scheduler = WaveScheduler(
                    run_id=RUN_ID,
                    phase=Phase.TRANSFORM,
                    store=store,
                    db=repository,
                    budgets=BudgetsSection(),
                    clock=clock,
                )
                return {
                    index: await scheduler.wave_state(index)
                    for index in await store.wave_indices(RUN_ID)
                }
            finally:
                await read_conn.close()

    return asyncio.run(compute())


# --------------------------------------------------------------------------------------
# 1. the whitelist
# --------------------------------------------------------------------------------------


def test_step_6_executes_only_the_whitelisted_verb_table_pairs(
    fleet: Path, step6_sql: SqlTrace  # noqa: F811
) -> None:
    """**Quantity watched:** the set of `(verb, table)` pairs executed inside `_unblock_dependents`.

    Asserted in BOTH directions, and both directions were validated (this lane's report):
    an extra pair is the synthetic fault and the value-preserving `UPDATE`; a missing pair is the
    known-bad state where the freed repo's membership is never moved.
    """
    code, payload = _resume(fleet)
    assert code == cli.ExitCode.USAGE, "the fixture's resume did not reach step 6"
    assert payload["unblocked_dependents"]["applied"] is True, "step 6 wrote nothing to observe"

    observed = step6_sql.writes()
    unresolved = {pair: sql for pair, sql in observed.items() if pair[1] == _UNRESOLVED}
    assert not unresolved, (
        "step 6 executed a statement this instrument cannot resolve to a table. It is reported "
        f"rather than assumed harmless: {sorted(unresolved.values())}"
    )
    extra = {pair: sql for pair, sql in observed.items() if pair not in STEP6_SQL_TARGETS}
    assert not extra, (
        "§11.5 step 6 wrote to a target outside `STEP6_SQL_TARGETS`: "
        + "; ".join(f"{pair} via {sql!r}" for pair, sql in sorted(extra.items()))
        + ". Add the pair to the whitelist only after deciding that step 6 may touch it."
    )
    missing = STEP6_SQL_TARGETS - set(observed)
    assert not missing, (
        f"step 6 no longer executes {sorted(missing)}. `('UPDATE', 'wave_members')` missing is "
        "the known-bad state: the dependents were un-blocked and left in the wave they were "
        "blocked in, which reads as a clean resume."
    )


# --------------------------------------------------------------------------------------
# 2. the before/after snapshot invariant, and the two instruments' agreement
# --------------------------------------------------------------------------------------


def test_step_6_moves_exactly_one_member_and_leaves_every_existing_wave_row_alone(
    fleet: Path, step6_sql: SqlTrace  # noqa: F811
) -> None:
    """**Quantities watched:** (a) every `waves` and `wave_members` row as a full value tuple,
    before and after; (b) the set of tables the trace says were written.

    (a) and (b) are genuinely different derivations — one compares stored rows, one counts
    executed statements — and the last assertion is that they agree. They have complementary
    blind spots: a value-preserving write is invisible to (a) and visible to (b); a write routed
    through an already-whitelisted pair is visible to (a) and invisible to (b).
    """
    db = fleet / "state" / "fleet.db"
    before = {table: _dump(db, table) for table in SNAPSHOT_TABLES}
    before_waves, before_members = before["waves"], _membership(db)
    code, _payload = _resume(fleet)
    assert code == cli.ExitCode.USAGE
    after = {table: _dump(db, table) for table in SNAPSHOT_TABLES}
    after_waves, after_members = after["waves"], _membership(db)

    survived = [row for row in after_waves if row in before_waves]
    assert survived == before_waves, (
        "an existing `waves` row was rewritten; step 6 may only INSERT one. Rows that changed: "
        f"{[row for row in before_waves if row not in after_waves]}"
    )
    appended = [row for row in after_waves if row not in before_waves]
    assert len(appended) == 1, f"step 6 appended {len(appended)} `waves` rows, not one"

    moved = {repo: (before_members[repo], wave) for repo, wave in after_members.items()
             if before_members[repo] != wave}
    assert set(moved) == {FREED}, f"the wrong members moved: {sorted(moved)}"
    assert moved[FREED][1] > max(before_members.values()), "the member did not move upward"
    assert set(after_members) == set(before_members), "a `wave_members` row appeared or vanished"

    changed = {table for table in SNAPSHOT_TABLES if before[table] != after[table]}
    assert changed == {table for _verb, table in step6_sql.writes()}, (
        "the row snapshot and the statement trace disagree about which tables step 6 wrote: "
        f"rows changed in {sorted(changed)}, statements executed against "
        f"{sorted({table for _verb, table in step6_sql.writes()})}"
    )


# --------------------------------------------------------------------------------------
# 3. `WaveState` is computed, so record what it BECOMES
# --------------------------------------------------------------------------------------


def test_the_appended_wave_computes_open_and_the_wave_it_emptied_stays_closed(
    fleet: Path,  # noqa: F811
) -> None:
    """**Quantity watched:** `WaveScheduler.wave_state` for every wave, at `Phase.TRANSFORM`.

    ADR-0089 §4's precedent, one step over: the `waves` DDL has no status column, so a wave's
    state is recomputed on every read and there is no end state to assert — only the value the
    production computation returns. Step 5 is disclosed there as *re-opening* the closed wave it
    demotes a member out of. Step 6 is the opposite shape and this records it: the freed repo
    leaves wave 2 for the appended wave 3, so wave 2 answers the same as before while wave 3 —
    the only wave holding a dispatchable member — answers `OPEN`.

    `Phase.TRANSFORM` is the phase the fixture's blocked rows carry, so the freed repo's row is
    `BLOCKED` (settled) before and `PENDING` (dispatchable) after: the two anchors differ, and
    the computed value can move.
    """
    db = fleet / "state" / "fleet.db"
    before = _wave_states(db)
    assert before == {0: WaveState.CLOSED, 1: WaveState.CLOSED, 2: WaveState.CLOSED}, (
        "the fixture no longer expresses the property: with a dispatchable member left in an "
        "earlier wave, `OPEN` on the appended wave would say nothing about step 6"
    )
    code, _payload = _resume(fleet)
    assert code == cli.ExitCode.USAGE
    after = _wave_states(db)
    assert after == {
        0: WaveState.CLOSED, 1: WaveState.CLOSED, 2: WaveState.CLOSED, 3: WaveState.OPEN
    }, f"the computed wave states after step 6 are {after}"


# --------------------------------------------------------------------------------------
# 4. the recognition step is its own attack surface
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("SELECT 1", None),
        ("  begin immediate ", None),
        ("PRAGMA foreign_keys = ON", None),
        ("pragma  main.busy_timeout = 30000", None),
        # a PRAGMA is classified by its ARGUMENT: these three change the database file
        ("PRAGMA user_version = 9", ("PRAGMA", _UNRESOLVED)),
        ("PRAGMA journal_mode = WAL", ("PRAGMA", _UNRESOLVED)),
        ("PRAGMA optimize", ("PRAGMA", _UNRESOLVED)),
        ("PRAGMA", ("PRAGMA", _UNRESOLVED)),
        # SQLite renders an internally generated nested statement as a `--` line
        ('-- ANALYZE "main"."t"', (_UNRESOLVED, _UNRESOLVED)),
        # quoting, schema qualification and case are resolved, not dropped
        ('UPDATE  "waves"  SET max_usd = 1', ("UPDATE", "waves")),
        ("insert or replace into main.waves (run_id) values (?)", ("INSERT", "waves")),
        ("DELETE\n  FROM wave_members\n WHERE run_id = ?", ("DELETE", "wave_members")),
        ("UPDATE /* moved here */ phases SET blocked_by = ?", ("UPDATE", "phases")),
        ("-- why\nUPDATE phases SET blocked_by = ?", ("UPDATE", "phases")),
        # a keyword inside an expanded parameter is a literal, not a verb
        ("UPDATE phases SET last_error = 'DROP TABLE waves'", ("UPDATE", "phases")),
        # forms this instrument cannot resolve are LOUD, never silently harmless
        ("WITH x AS (SELECT 1) INSERT INTO waves SELECT * FROM x", ("WITH", _UNRESOLVED)),
        ("VACUUM", ("VACUUM", _UNRESOLVED)),
        ("INSERT (a) VALUES (1)", ("INSERT", _UNRESOLVED)),
        ("CREATE TABLE IF NOT EXISTS shadow_waves (a)", ("CREATE", "shadow_waves")),
    ],
)
def test_the_recognition_step_resolves_or_says_it_cannot(
    statement: str, expected: tuple[str, str] | None
) -> None:
    """The pre-filter is a separate attack surface from the whitelist it feeds.

    A detector that drops what it cannot parse goes silent on exactly the site it exists to
    catch — the shape that took an unrelated census from 27 to 26 when one `INSERT` was reflowed
    into a triple-quoted string. Every case here is either resolved or reported as `"?"`, and
    `test_step_6_executes_only_the_whitelisted_verb_table_pairs` fails on a `"?"`.
    """
    assert write_target(statement) == expected


def test_every_pragma_the_connection_factory_issues_is_classified() -> None:
    """`_NON_WRITE_PRAGMAS` is cross-checked against the declaration that produces the statements.

    `_NON_WRITE_PRAGMAS` is a literal precisely so that it is not derived from
    `PER_CONNECTION_PRAGMAS`: a set derived from the tuple would exempt whatever the tuple grew,
    which is the one edit that can put a writing PRAGMA on every connection the run opens. Two
    independently written sets, compared by equality, are loud in both directions instead — a
    pragma added to the tuple is unclassified, a name left here that production stopped issuing is
    stale.

    **What this cannot do**, stated rather than implied: it forces a classification decision, it
    does not check that the decision is right. An author who adds `PRAGMA user_version = 9` to the
    tuple and then adds `user_version` here satisfies it. What catches *that* is
    `test_step_6_executes_only_the_whitelisted_verb_table_pairs`, which sees the statement itself;
    the two are complementary, not redundant — this one fires on a pragma added to a code path
    step 6 never reaches, that one on a pragma issued anywhere inside the window whether or not it
    came from this tuple.
    """
    issued: dict[str | None, str] = {}
    for statement in PER_CONNECTION_PRAGMAS:
        tokens = _tokens(statement)
        assert tokens and tokens[0].upper() == "PRAGMA", (
            f"`PER_CONNECTION_PRAGMAS` holds a statement this module cannot read as a PRAGMA: "
            f"{statement!r}"
        )
        issued[_resolve_table(tokens[1:])] = statement
    assert set(issued) == _NON_WRITE_PRAGMAS, (
        "the PRAGMAs production issues on every connection and the PRAGMAs this module classifies "
        "as non-writes have drifted apart. Unclassified (production issues it, this module would "
        f"report it as `('PRAGMA', '?')`): "
        f"{sorted(str(n) for n in set(issued) - _NON_WRITE_PRAGMAS)}. "
        f"Stale (classified here, no longer issued): {sorted(_NON_WRITE_PRAGMAS - set(issued))}. "
        "Decide whether the new one writes — measure it, do not assume the keyword — before "
        "adding it here."
    )
