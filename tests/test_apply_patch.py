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
    commit_to_file_edits,
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


def _edit(edits: list[dict[str, str]], path: str) -> dict[str, str]:
    matches = [e for e in edits if e["path"] == path]
    assert len(matches) == 1, f"expected exactly one edit for {path!r}, got {matches}"
    return matches[0]


def test_add_produces_a_dev_null_to_b_diff() -> None:
    text = "*** Begin Patch\n*** Add File: hello.txt\n+Hello world\n*** End Patch"
    patch, _ = text_to_patch(text, orig={})
    commit = patch_to_commit(patch, orig={})
    edits = commit_to_file_edits(commit)
    edit = _edit(edits, "hello.txt")
    assert edit["diff"].startswith("--- /dev/null\n+++ b/hello.txt\n")
    assert "+Hello world" in edit["diff"]


def test_delete_produces_an_a_to_dev_null_diff() -> None:
    text = "*** Begin Patch\n*** Delete File: obsolete.txt\n*** End Patch"
    orig = {"obsolete.txt": "old contents\n"}
    patch, _ = text_to_patch(text, orig)
    commit = patch_to_commit(patch, orig)
    edits = commit_to_file_edits(commit)
    edit = _edit(edits, "obsolete.txt")
    assert edit["diff"].startswith("--- a/obsolete.txt\n+++ /dev/null\n")
    assert "-old contents" in edit["diff"]


def test_update_without_move_uses_the_same_path_on_both_sides() -> None:
    text = (
        "*** Begin Patch\n*** Update File: src/app.py\n@@ def greet():\n"
        '-    print("Hi")\n+    print("Hello, world!")\n*** End Patch'
    )
    orig = {"src/app.py": 'def greet():\n    print("Hi")\n'}
    patch, _ = text_to_patch(text, orig)
    commit = patch_to_commit(patch, orig)
    edits = commit_to_file_edits(commit)
    edit = _edit(edits, "src/app.py")
    assert edit["diff"].startswith("--- a/src/app.py\n+++ b/src/app.py\n")


def test_update_with_move_diffs_old_path_against_new_path() -> None:
    text = (
        "*** Begin Patch\n*** Update File: src/app.py\n*** Move to: src/main.py\n"
        '@@ def greet():\n-    print("Hi")\n+    print("Hello, world!")\n*** End Patch'
    )
    orig = {"src/app.py": 'def greet():\n    print("Hi")\n'}
    patch, _ = text_to_patch(text, orig)
    commit = patch_to_commit(patch, orig)
    edits = commit_to_file_edits(commit)
    # The post-image path is src/main.py: no more entry keyed on the pre-image path.
    assert [e["path"] for e in edits] == ["src/main.py"]
    edit = edits[0]
    # For renames, the diff includes git's extended rename header for git apply compatibility
    assert edit["diff"].startswith("diff --git a/src/app.py b/src/main.py\n")
    assert "rename from src/app.py\n" in edit["diff"]
    assert "rename to src/main.py\n" in edit["diff"]
    assert "--- a/src/app.py\n+++ b/src/main.py\n" in edit["diff"]


def test_identical_before_and_after_still_produces_no_hunk_when_only_moved() -> None:
    """A pure rename with no content change: `Update File` + `Move to` and nothing else. The
    diff still names both paths (so `diff_paths` can recover the destination) but carries no
    hunk lines, matching `difflib.unified_diff`'s own behaviour on identical inputs."""
    text = "*** Begin Patch\n*** Update File: a.txt\n*** Move to: b.txt\n*** End Patch"
    orig = {"a.txt": "unchanged\n"}
    patch, _ = text_to_patch(text, orig)
    commit = patch_to_commit(patch, orig)
    edits = commit_to_file_edits(commit)
    edit = _edit(edits, "b.txt")
    assert "@@" not in edit["diff"]


def test_commit_to_file_edits_round_trips_through_a_real_git_apply(tmp_path) -> None:
    """The authoritative check (CLAUDE.md guardrail 6: verify in the environment that will run
    it): a real `git apply --index` in a scratch repo, matching production's behavior
    (src/fleet/vcs/git.py:432). Tests both content changes and renames via git's extended
    rename headers."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)  # noqa: S607
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)  # noqa: S607
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)  # noqa: S607
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text('def greet():\n    print("Hi")\n', encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)  # noqa: S607
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)  # noqa: S607

    # Test UPDATE with move (rename + content changes) using git apply --index
    text = (
        "*** Begin Patch\n*** Update File: src/app.py\n*** Move to: src/main.py\n"
        '@@ def greet():\n-    print("Hi")\n+    print("Hello, world!")\n*** End Patch'
    )
    orig = {"src/app.py": 'def greet():\n    print("Hi")\n'}
    patch, _ = text_to_patch(text, orig)
    commit = patch_to_commit(patch, orig)
    edits = commit_to_file_edits(commit)
    assert len(edits) == 1
    edit = edits[0]
    assert edit["path"] == "src/main.py"

    patch_file = tmp_path / "the.patch"
    patch_file.write_text(edit["diff"], encoding="utf-8")
    # Use git apply --index, matching production's actual behavior
    subprocess.run(["git", "apply", "--index", str(patch_file)], cwd=repo, check=True)  # noqa: S603, S607

    # Verify the old path is gone and the new path has the correct content
    assert not (repo / "src" / "app.py").exists()
    assert (repo / "src" / "main.py").read_text(encoding="utf-8") == (
        'def greet():\n    print("Hello, world!")\n'
    )


_WRITERS = frozenset({"apply_commit", "apply_patch", "write_file", "remove_file"})
_MODULE = "fleet.vcs.apply_patch"


def _writer_imports(source: str) -> list[str]:
    """Every way `source` reaches one of `_WRITERS` in `fleet.vcs.apply_patch`: a `from <module>
    import <writer>`, or an attribute `<alias>.<writer>` on a name bound to the module by
    `import fleet.vcs.apply_patch [as X]` / `from fleet.vcs import apply_patch [as X]`. Read from
    the AST, so a comment or string mentioning a writer is not an import."""
    import ast

    tree = ast.parse(source)
    module_aliases: set[str] = set()
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == _MODULE:
            hits += [
                f"{node.lineno}: from {_MODULE} import {a.name}"
                for a in node.names
                if a.name in _WRITERS or a.name == "*"
            ]
        elif isinstance(node, ast.ImportFrom) and node.module == "fleet.vcs":
            module_aliases |= {a.asname or a.name for a in node.names if a.name == "apply_patch"}
        elif isinstance(node, ast.Import):
            module_aliases |= {a.asname or a.name for a in node.names if a.name == _MODULE}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _WRITERS:
            owner = ast.unparse(node.value)
            if owner in module_aliases:
                hits.append(f"{node.lineno}: {owner}.{node.attr}")
    return hits


def test_nothing_under_src_but_apply_patch_itself_reaches_its_filesystem_writers() -> None:
    """`git apply` is the harness's ONLY worktree writer; this module's `apply_commit`/
    `apply_patch` and their default `write_file`/`remove_file` write straight to disk. The
    module docstring states nothing else calls them — this enforces it instead of trusting it."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "fleet"
    own = src / "vcs" / "apply_patch.py"
    offenders = {
        str(path.relative_to(src)): hits
        for path in sorted(src.rglob("*.py"))
        if path != own and (hits := _writer_imports(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}


@pytest.mark.parametrize(
    "source",
    [
        "from fleet.vcs.apply_patch import apply_commit\n",
        "from fleet.vcs.apply_patch import text_to_patch, write_file as w\n",
        "from fleet.vcs.apply_patch import *\n",
        "from fleet.vcs import apply_patch\napply_patch.apply_patch('x', open, print, print)\n",
        "import fleet.vcs.apply_patch as ap\nap.remove_file('x')\n",
    ],
)
def test_the_writer_sweep_fires_on_each_import_form(source: str) -> None:
    assert _writer_imports(source)


def test_the_writer_sweep_is_silent_on_the_pure_functions_comments_and_strings() -> None:
    source = (
        "from fleet.vcs.apply_patch import text_to_patch, patch_to_commit\n"
        "# never call apply_commit here\n"
        "NOTE = 'apply_patch.write_file is forbidden'\n"
    )
    assert _writer_imports(source) == []
