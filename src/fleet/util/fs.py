"""Filesystem primitives with crash-safe semantics (SPEC §8 `util/fs.py`).

`atomic_write` is the only sanctioned way to produce a durable file in this harness:
`migration_state.json`, every `artifacts/` payload and every write-ahead patch file go
through it, because a half-written checkpoint is indistinguishable from a corrupt one.

**And crash-safety has a precondition this module is also where it gets checked: room.**
`atomic_write` cannot make an `ENOSPC` atomic, and neither can SQLite: §13 row 42 is the failure
where 250 mirrors plus an unpruned Bazel cache fill the volume and the write that fails is the
one inside a `BEGIN IMMEDIATE`, voiding the "every non-zero exit leaves a valid checkpoint"
guarantee (§10). So `free_bytes` / `require_free_space` live here, next to the writes they
protect, and every operation that consumes gigabytes — a clone, a container start, a Bazel
invocation — asks BEFORE it starts rather than reporting `ENOSPC` afterwards.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def atomic_write(
    path: str | os.PathLike[str],
    data: str | bytes,
    *,
    encoding: str = "utf-8",
    fsync: bool = True,
) -> Path:
    """Write `data` to `path` atomically: temp file in the same directory, then `os.replace`.

    `os.replace` is atomic within a filesystem, so a reader either sees the whole previous
    file or the whole new one — never a truncated prefix. The temp file is created in the
    destination directory precisely so the rename never crosses a filesystem boundary.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = data.encode(encoding) if isinstance(data, str) else data

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            if fsync:
                os.fsync(fh.fileno())
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if fsync:
        _fsync_dir(target.parent)
    return target


def _fsync_dir(directory: Path) -> None:
    """Durably record the rename itself, not just the bytes."""
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except OSError:  # pragma: no cover - platforms without directory fds
        return
    try:
        os.fsync(dir_fd)
    except OSError:  # pragma: no cover - some filesystems refuse dir fsync
        pass
    finally:
        os.close(dir_fd)


class DiskFloorBreached(OSError):
    """`preflight.min_free_bytes` would be breached by the operation about to start (§11.3).

    An `OSError` on purpose: it is the same category as the `ENOSPC` it exists to pre-empt, so
    the `except OSError` a worker already wraps its filesystem work in catches it — but it is a
    distinct type, so the paths that must map it to `FailureClass.DISK_EXHAUSTED` and exit 9 can
    tell it apart from an ordinary I/O error. The free space and the floor are attributes, not
    only prose in the message, because a caller that has to regex a string to report a number is
    a caller that will eventually report the wrong one.
    """

    def __init__(self, *, path: Path, free: int, floor: int, operation: str) -> None:
        super().__init__(
            f"{operation} refused: {path} has {free} bytes free, under "
            f"preflight.min_free_bytes ({floor}); short by {floor - free} bytes. "
            "§11.3/§13 row 42 — ENOSPC is detected before the write that would fail, not "
            "reported by the transaction it corrupted."
        )
        self.path = path
        self.free = free
        self.floor = floor
        self.operation = operation


def free_bytes(path: str | os.PathLike[str]) -> int:
    """Bytes free on the filesystem holding `path`, or holding its nearest existing ancestor.

    The ancestor walk is not a convenience: the check has to happen BEFORE the directory the
    operation would create exists, and `shutil.disk_usage` on a path that is not there raises.
    A cache directory that does not exist yet still sits on a volume with a real amount of room.
    """
    probe = Path(path)
    while True:
        try:
            return shutil.disk_usage(probe).free
        except OSError:
            parent = probe.parent
            if parent == probe:
                raise
            probe = parent


def require_free_space(
    path: str | os.PathLike[str], min_free_bytes: int, *, operation: str
) -> None:
    """Refuse `operation` if `path`'s volume is below the floor. `min_free_bytes <= 0` disables.

    Zero is "unchecked" rather than "always passes" so that a payload built by hand in a test —
    which is not a fleet run and has no `config/fleet.yaml` behind it — is not gated by a 50 GiB
    default it never asked for, while every payload the CLI builds carries the configured floor.
    The disabled case is therefore visible in the payload itself, not hidden in a branch here.
    """
    if min_free_bytes <= 0:
        return
    available = free_bytes(path)
    if available < min_free_bytes:
        raise DiskFloorBreached(
            path=Path(path), free=available, floor=min_free_bytes, operation=operation
        )


@contextmanager
def scoped_tempdir(
    prefix: str = "fleet-", parent: str | os.PathLike[str] | None = None
) -> Iterator[Path]:
    """A temp directory that is removed on exit, including after an exception."""
    created = Path(tempfile.mkdtemp(prefix=prefix, dir=None if parent is None else str(parent)))
    try:
        yield created
    finally:
        shutil.rmtree(created, ignore_errors=True)
