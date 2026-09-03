"""`util/cgroup.py`: the process-tree cgroup v2 memory reader (SPEC §12.22 RSS-sampling
sub-clause, task B1).

Every test here points the reader at a REAL temp file / real temp directory tree it builds by
hand -- no mocking of `open()`, per the task brief -- so `Path.read_text()` genuinely exercises
the real filesystem read path, only at a synthetic location instead of the real host's.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fleet.util.cgroup import (
    CgroupUnavailableError,
    read_process_tree_memory_bytes,
    resolve_own_cgroup_v2_relpath,
)


def _write_cgroup_file(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "proc_self_cgroup"
    path.write_text(content)
    return path


def test_resolves_a_real_v2_single_line(tmp_path: Path) -> None:
    """The documented, confirmed-live shape (research-18): one `0::<path>` line."""
    assert (
        resolve_own_cgroup_v2_relpath(
            proc_self_cgroup=_write_cgroup_file(
                tmp_path, "0::/user.slice/user-1000.slice/session-5.scope\n"
            )
        )
        == "/user.slice/user-1000.slice/session-5.scope"
    )


def test_reads_the_correct_integer_from_a_real_resolved_path(tmp_path: Path) -> None:
    """End-to-end: a real `/proc/self/cgroup`-shaped file pointing at a real nested
    `memory.current` file under a real `cgroup_fs_root` tree, exactly the two-step resolution the
    reader performs against the real host."""
    cgroup_file = _write_cgroup_file(tmp_path, "0::/user.slice/session-9.scope\n")
    cgroup_root = tmp_path / "sys_fs_cgroup"
    leaf = cgroup_root / "user.slice" / "session-9.scope"
    leaf.mkdir(parents=True)
    (leaf / "memory.current").write_text("123456789\n")

    result = read_process_tree_memory_bytes(
        proc_self_cgroup=cgroup_file, cgroup_fs_root=cgroup_root
    )

    assert result == 123456789
    assert isinstance(result, int)


def test_missing_proc_self_cgroup_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(CgroupUnavailableError, match="could not read"):
        resolve_own_cgroup_v2_relpath(proc_self_cgroup=tmp_path / "does-not-exist")


def test_v1_style_multiline_file_is_explicitly_unsupported(tmp_path: Path) -> None:
    """A pure cgroup v1 host: multiple numbered-hierarchy lines, no `0::` line. Per the module's
    disclosed assumption this must fail loudly, not silently pick one line or guess a v1 path."""
    v1_content = "12:memory:/user.slice\n11:cpu,cpuacct:/user.slice\n10:pids:/user.slice\n"
    with pytest.raises(CgroupUnavailableError, match="cgroup v2"):
        resolve_own_cgroup_v2_relpath(proc_self_cgroup=_write_cgroup_file(tmp_path, v1_content))


def test_two_v2_lines_is_malformed_and_refused(tmp_path: Path) -> None:
    """A file with more than one `0::` line is not a shape this module has a basis to interpret
    -- refuse rather than silently taking the first."""
    with pytest.raises(CgroupUnavailableError, match="found 2"):
        resolve_own_cgroup_v2_relpath(
            proc_self_cgroup=_write_cgroup_file(
                tmp_path, "0::/user.slice/a.scope\n0::/user.slice/b.scope\n"
            )
        )


def test_missing_memory_current_at_resolved_path_fails_loudly(tmp_path: Path) -> None:
    cgroup_file = _write_cgroup_file(tmp_path, "0::/user.slice/session-1.scope\n")
    cgroup_root = tmp_path / "sys_fs_cgroup"
    (cgroup_root / "user.slice" / "session-1.scope").mkdir(parents=True)
    # deliberately no memory.current written

    with pytest.raises(CgroupUnavailableError, match="could not read"):
        read_process_tree_memory_bytes(proc_self_cgroup=cgroup_file, cgroup_fs_root=cgroup_root)


def test_non_integer_memory_current_fails_loudly(tmp_path: Path) -> None:
    cgroup_file = _write_cgroup_file(tmp_path, "0::/user.slice/session-2.scope\n")
    cgroup_root = tmp_path / "sys_fs_cgroup"
    leaf = cgroup_root / "user.slice" / "session-2.scope"
    leaf.mkdir(parents=True)
    (leaf / "memory.current").write_text("not-a-number\n")

    with pytest.raises(CgroupUnavailableError, match="bare integer"):
        read_process_tree_memory_bytes(proc_self_cgroup=cgroup_file, cgroup_fs_root=cgroup_root)


def test_this_process_own_real_cgroup_resolves_and_reads_cleanly() -> None:
    """No injection at all -- the real defaults, against THIS process's real, live cgroup v2
    file. This is the one test that touches the actual host paths (never `docker`): it proves
    the reader works against the real environment research-18 confirmed is cgroup v2, not only
    against synthetic fixtures. If this test is ever run on a v1-only host it is expected to
    raise `CgroupUnavailableError` rather than pass silently with a wrong number -- that would be
    a real, disclosed environment difference, not a bug in this test."""
    result = read_process_tree_memory_bytes()
    assert isinstance(result, int)
    assert result > 0
