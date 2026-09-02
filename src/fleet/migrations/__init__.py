"""The forward-only migration ladder and the `fleet migrate-db` runner (SPEC §6).

**Migrations run here and nowhere else.** §6's migration policy is normative: DDL is *not* applied
implicitly at worker startup, because `CREATE TABLE IF NOT EXISTS` creates a *missing* schema and
never executes an `ALTER` or a rebuild — a startup path that "applies `schema.sql` idempotently"
silently leaves an old database at its old shape while reporting success. Workers read
`PRAGMA user_version` at startup and **refuse to start** if it differs from the version compiled
into the harness (`fleet.models.state.SCHEMA_VERSION`); refusing is their whole contribution.

**The runner, statement for statement (§6).** Per step: `BEGIN EXCLUSIVE` → re-read
`PRAGMA user_version` **inside** the transaction → apply exactly that version's step → verify
`PRAGMA foreign_key_check` returns zero rows → set `user_version` → `COMMIT`. **One transaction
per step.** The in-transaction re-read is not belt-and-braces: a version read before the lock is
taken is a race, and its loser applies a ladder that another process has already applied. Two
concurrent `fleet migrate-db` invocations are therefore safe — `BEGIN EXCLUSIVE` serializes them
and the loser observes the migrated version and no-ops.

`PRAGMA foreign_keys` is set **OFF before the transaction opens** and stays off for the whole
ladder. §6 requires it for every table-rebuild path — on `ALTER TABLE … RENAME` SQLite silently
rewrites child `REFERENCES` clauses to point at the new name — and the PRAGMA is a **no-op inside
a transaction**, so a step cannot set it for itself. `PRAGMA foreign_key_check` before each commit
is what replaces the enforcement that was switched off.

**Why stdlib `sqlite3`, not `aiosqlite`.** A migration is a one-shot administrative operation
holding an exclusive lock on the whole database: there is nothing to interleave, so asynchrony
buys no concurrency here. Routing it through the async writer would be actively worse — it would
mean the single `StateWriter` actor (§11.5), whose transactions are contractually bounded to
<50 ms of pure SQL, taking `BEGIN EXCLUSIVE` for a multi-second table rebuild and stalling every
other write in the process behind it. `fleet migrate-db` runs when nothing else is running.

**Forward-only.** There is no downgrade path and none is planned: a database ahead of this
harness is refused, not rewound (`MigrationVersionError`). A `user_version` *gap* is never a
reason to discard an in-flight run — it is a "not yet", closable by running this ladder (§6,
ADR-0036).

**Fresh databases never come here.** They get `state/schema.sql` and land directly at 8 via
`fleet.state.db.initialize_database()`; this ladder exists only for databases that already hold
data, and `migrate()` refuses `user_version = 0` by name rather than pretending to upgrade a
database with no schema (CLAUDE.md Rule 11).

**Discovery is the explicit tuple below**, never a filesystem glob: a glob orders steps by whatever
`readdir` returns, and a mis-ordered ladder corrupts data silently. `STEPS` is validated at import
for strict ordering and contiguity.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from fleet.migrations import (
    v002_node_kind,
    v003_anti_anchoring,
    v004_stub_lifecycle,
    v005_backend_identity,
    v006_mutations_deleted,
    v007_logical_keys,
    v008_reservations,
    v009_coordinate_version,
)

__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "EARLIEST_MIGRATABLE_VERSION",
    "LATEST_VERSION",
    "STEPS",
    "MigrationError",
    "MigrationIntegrityError",
    "MigrationStep",
    "MigrationStepError",
    "MigrationVersionError",
    "current_version",
    "migrate",
]

#: Long, because the loser of two concurrent `fleet migrate-db` calls must WAIT for the winner's
#: rebuild and then observe the migrated version — not fail with SQLITE_BUSY and be re-run by hand.
DEFAULT_BUSY_TIMEOUT_MS: Final = 300_000


class MigrationError(RuntimeError):
    """Base class for every failure this package raises. Never caught-and-ignored internally."""


class MigrationVersionError(MigrationError):
    """`user_version` is one this ladder cannot act on: ahead of it, or an unschema'd 0."""


class MigrationStepError(MigrationError):
    """A step raised. Its transaction is rolled back and `user_version` is unchanged (Rule 11)."""


class MigrationIntegrityError(MigrationError):
    """`PRAGMA foreign_key_check` returned rows before the commit (§6). The step is rolled back."""


@dataclass(frozen=True, slots=True)
class MigrationStep:
    """One rung: the `user_version` it *produces* and the callable that produces it."""

    version: int
    upgrade: Callable[[sqlite3.Connection], None]
    module: str


#: THE registry. Ordered, contiguous, explicit — one entry per `vNNN_*.py` module (§6).
STEPS: Final[tuple[MigrationStep, ...]] = (
    MigrationStep(v002_node_kind.VERSION, v002_node_kind.upgrade, v002_node_kind.__name__),
    MigrationStep(
        v003_anti_anchoring.VERSION, v003_anti_anchoring.upgrade, v003_anti_anchoring.__name__
    ),
    MigrationStep(
        v004_stub_lifecycle.VERSION, v004_stub_lifecycle.upgrade, v004_stub_lifecycle.__name__
    ),
    MigrationStep(
        v005_backend_identity.VERSION,
        v005_backend_identity.upgrade,
        v005_backend_identity.__name__,
    ),
    MigrationStep(
        v006_mutations_deleted.VERSION,
        v006_mutations_deleted.upgrade,
        v006_mutations_deleted.__name__,
    ),
    MigrationStep(
        v007_logical_keys.VERSION, v007_logical_keys.upgrade, v007_logical_keys.__name__
    ),
    MigrationStep(
        v008_reservations.VERSION, v008_reservations.upgrade, v008_reservations.__name__
    ),
    MigrationStep(
        v009_coordinate_version.VERSION,
        v009_coordinate_version.upgrade,
        v009_coordinate_version.__name__,
    ),
)


def _validate_registry(steps: Sequence[MigrationStep]) -> None:
    """Strictly ascending and contiguous, or the ladder is a lie. Checked at import time."""
    versions = [step.version for step in steps]
    if versions != sorted(set(versions)):
        raise MigrationError(f"migration steps are not strictly ascending/unique: {versions}")
    expected = list(range(versions[0], versions[0] + len(versions)))
    if versions != expected:
        raise MigrationError(f"migration ladder has a gap: {versions} != {expected}")


_validate_registry(STEPS)

#: The version the ladder ends at. Must equal §5 `SCHEMA_VERSION` and `schema.sql`'s baseline.
LATEST_VERSION: Final = STEPS[-1].version
#: The oldest `user_version` this ladder can lift. Older databases predate the harness's history.
EARLIEST_MIGRATABLE_VERSION: Final = STEPS[0].version - 1


# --------------------------------------------------------------------------------------
# the runner
# --------------------------------------------------------------------------------------


def _read_user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    if row is None:  # pragma: no cover — PRAGMA user_version always returns a row
        raise MigrationError("PRAGMA user_version returned no row")
    return int(row[0])


def _open(path: Path, *, busy_timeout_ms: int) -> sqlite3.Connection:
    """Open the migration connection with `foreign_keys` OFF *before* any transaction (§6).

    `isolation_level=None` puts the driver in autocommit mode, so the only transactions on this
    connection are the `BEGIN EXCLUSIVE` blocks below and never one the driver inserted.
    """
    if not path.exists():
        raise MigrationVersionError(
            f"{path} does not exist; a fresh database is created by "
            "fleet.state.db.initialize_database() from schema.sql, not by the ladder (§6)"
        )
    conn = sqlite3.connect(path, isolation_level=None, timeout=busy_timeout_ms / 1000)
    conn.execute("PRAGMA foreign_keys = OFF")  # for the WHOLE ladder; a no-op once in a txn
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    return conn


def _select_step(current: int, steps: Sequence[MigrationStep]) -> MigrationStep | None:
    """The one step that applies at `current`, or None when the database is at the target."""
    target = steps[-1].version
    if current == target:
        return None
    if current > target:
        raise MigrationVersionError(
            f"database is at user_version={current}, ahead of this harness's {target}. Migrations "
            "are FORWARD-ONLY (§6): there is no downgrade path. Upgrade the harness instead."
        )
    if current == 0:
        raise MigrationVersionError(
            "user_version=0: this database carries no schema. A fresh database is created from "
            "schema.sql by fleet.state.db.initialize_database() and lands directly at "
            f"{target}; the ladder exists only for databases that already hold data (§6)."
        )
    if current < steps[0].version - 1:
        raise MigrationVersionError(
            f"database is at user_version={current}, older than the earliest version this ladder "
            f"can lift ({steps[0].version - 1}). No forward path exists."
        )
    for step in steps:
        if step.version > current:
            return step
    raise MigrationError(f"no step produces a version above {current}")  # pragma: no cover


def _foreign_key_check(conn: sqlite3.Connection, step: MigrationStep) -> None:
    """§6: `PRAGMA foreign_key_check` MUST return zero rows before the commit."""
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise MigrationIntegrityError(
            f"{step.module} left {len(violations)} foreign-key violation(s); first: "
            f"{violations[0]!r}. Rolled back — user_version stays below {step.version} (§6)."
        )


def _apply_one_step(
    conn: sqlite3.Connection, steps: Sequence[MigrationStep]
) -> tuple[int, MigrationStep | None]:
    """One `BEGIN EXCLUSIVE` transaction: re-read, apply at most one step, commit.

    Returns `(version observed under the lock, the step applied or None)`. The observed version is
    read **inside** the transaction and is the only version any decision here is made on.
    """
    conn.execute("BEGIN EXCLUSIVE")
    step: MigrationStep | None = None
    try:
        current = _read_user_version(conn)
        step = _select_step(current, steps)
        if step is not None:
            try:
                step.upgrade(conn)
            except Exception as exc:
                raise MigrationStepError(
                    f"{step.module} (user_version {current} → {step.version}) failed: {exc}. "
                    "Its transaction is rolled back and user_version is unchanged."
                ) from exc
            _foreign_key_check(conn, step)
            conn.execute(f"PRAGMA user_version = {int(step.version)}")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return current, step


def migrate(
    path: Path,
    *,
    steps: Sequence[MigrationStep] = STEPS,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> tuple[int, int]:
    """Run the ladder against `path`. Returns `(from_version, to_version)`.

    `from_version` is the version observed under the *first* exclusive lock, not a pre-flight
    read — so two concurrent callers report honestly: the winner returns `(7, 8)` and the loser,
    which acquires the lock after the winner commits, returns `(8, 8)` and does nothing.

    This is the whole of `fleet migrate-db` for a database that already holds data. It is the ONLY
    supported way to change an existing database's shape (§6).
    """
    conn = _open(path, busy_timeout_ms=busy_timeout_ms)
    try:
        observed_first: int | None = None
        while True:
            observed, applied = _apply_one_step(conn, steps)
            if observed_first is None:
                observed_first = observed
            if applied is None:
                return observed_first, observed
    finally:
        conn.close()


def current_version(path: Path) -> int:
    """`PRAGMA user_version` of `path`, read-only. This is what a worker compares and refuses on."""
    if not path.exists():
        raise MigrationVersionError(f"{path} does not exist")
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    try:
        return _read_user_version(conn)
    finally:
        conn.close()
