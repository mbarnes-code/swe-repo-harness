"""ADR-0145 — `src/fleet/vcs/apply_patch.py`, vendored from
`references/gpt-oss/gpt_oss/tools/apply_patch.py` (see that module's own docstring for the exact
provenance and adaptation notes).

Every test here is pure text-in/text-out: no Harmony, no vLLM, no network. This module is
exercised standalone before `harmony_gpt_oss.py` (Task 3+) exists at all.
"""

from __future__ import annotations

import pytest

from fleet.vcs.apply_patch import (
    ActionType,
    DiffError,
    identify_files_added,
    identify_files_needed,
    patch_to_commit,
    text_to_patch,
)


def test_add_file_creates_new_content() -> None:
    text = "*** Begin Patch\n*** Add File: hello.txt\n+Hello world\n*** End Patch"
    patch, fuzz = text_to_patch(text, orig={})
    commit = patch_to_commit(patch, orig={})
    assert fuzz == 0
    change = commit.changes["hello.txt"]
    assert change.type is ActionType.ADD
    assert change.new_content == "Hello world"
    assert change.old_content is None


def test_delete_file_carries_old_content_for_the_diff_step() -> None:
    text = "*** Begin Patch\n*** Delete File: obsolete.txt\n*** End Patch"
    orig = {"obsolete.txt": "old contents\n"}
    patch, _ = text_to_patch(text, orig)
    commit = patch_to_commit(patch, orig)
    change = commit.changes["obsolete.txt"]
    assert change.type is ActionType.DELETE
    assert change.old_content == "old contents\n"
    assert change.new_content is None


def test_update_file_applies_a_hunk_against_exact_context() -> None:
    text = (
        "*** Begin Patch\n"
        "*** Update File: src/app.py\n"
        "@@ def greet():\n"
        '-    print("Hi")\n'
        '+    print("Hello, world!")\n'
        "*** End Patch"
    )
    orig = {"src/app.py": 'def greet():\n    print("Hi")\n'}
    patch, fuzz = text_to_patch(text, orig)
    commit = patch_to_commit(patch, orig)
    change = commit.changes["src/app.py"]
    assert change.type is ActionType.UPDATE
    assert change.old_content == orig["src/app.py"]
    assert change.new_content == 'def greet():\n    print("Hello, world!")\n'
    assert change.move_path is None
    assert fuzz == 0


def test_update_file_with_move_carries_the_new_path() -> None:
    text = (
        "*** Begin Patch\n"
        "*** Update File: src/app.py\n"
        "*** Move to: src/main.py\n"
        "@@ def greet():\n"
        '-    print("Hi")\n'
        '+    print("Hello, world!")\n'
        "*** End Patch"
    )
    orig = {"src/app.py": 'def greet():\n    print("Hi")\n'}
    patch, _ = text_to_patch(text, orig)
    commit = patch_to_commit(patch, orig)
    change = commit.changes["src/app.py"]
    assert change.move_path == "src/main.py"
    assert change.new_content == 'def greet():\n    print("Hello, world!")\n'


def test_ambiguous_context_tolerates_a_trailing_whitespace_mismatch() -> None:
    """`find_context_core` (vendored, unchanged) tries exact match, then rstrip-tolerant, then
    fully stripped — each looser pass raises `fuzz`, never fails outright while a looser match
    exists. This pins that the vendored fuzz behaviour survived the vendoring unchanged."""
    text = (
        "*** Begin Patch\n"
        "*** Update File: f.txt\n"
        "@@\n"
        "-old line   \n"  # trailing spaces the hunk's context line does NOT have
        "+new line\n"
        "*** End Patch"
    )
    orig = {"f.txt": "old line\n"}
    patch, fuzz = text_to_patch(text, orig)
    commit = patch_to_commit(patch, orig)
    assert commit.changes["f.txt"].new_content == "new line\n"
    assert fuzz >= 1


def test_missing_end_patch_sentinel_raises_diff_error() -> None:
    with pytest.raises(DiffError, match="missing sentinels"):
        text_to_patch("*** Begin Patch\n*** Add File: a.txt\n+x\n", orig={})


def test_missing_begin_patch_sentinel_raises_diff_error() -> None:
    with pytest.raises(DiffError, match="missing sentinels"):
        text_to_patch("*** Add File: a.txt\n+x\n*** End Patch", orig={})


def test_update_of_a_file_not_in_orig_raises_diff_error() -> None:
    with pytest.raises(DiffError, match="missing file"):
        text_to_patch(
            "*** Begin Patch\n*** Update File: nope.txt\n@@\n-a\n+b\n*** End Patch",
            orig={},
        )


def test_identify_files_needed_covers_update_and_delete_not_add() -> None:
    text = (
        "*** Begin Patch\n"
        "*** Add File: new.txt\n+x\n"
        "*** Update File: u.txt\n@@\n-a\n+b\n"
        "*** Delete File: d.txt\n"
        "*** End Patch"
    )
    assert set(identify_files_needed(text)) == {"u.txt", "d.txt"}
    assert identify_files_added(text) == ["new.txt"]
