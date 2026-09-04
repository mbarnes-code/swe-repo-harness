"""§12.36 / ADR-0021, §3.2 step 5: `rewrite/approach.py`'s `compute_approach_signature`.

The one property this module exists for: "the same idea, retyped" collides, and a genuinely
different change does not. Every test here is built against that, using the REAL vendored
`tools/bin/ast-grep` (conftest puts it on PATH, no skip on this boundary — see
`tests/test_rewrite.py`'s precedent) for the structural half of the classification.
"""

from __future__ import annotations

import asyncio

import pytest

from fleet.rewrite.approach import compute_approach_signature

DEST = "java/com/acme/billing"


def _sig(diffs: list[str]) -> str:
    return asyncio.run(compute_approach_signature(diffs))


# =======================================================================================
# 1. the collision guarantee: same idea, retyped
# =======================================================================================
def test_a_reindented_reordered_offset_shifted_repeat_collides_with_the_original() -> None:
    """SPEC's own scenario (docs/SPEC.md §12.36): re-indented, re-ordered, shifted hunk offsets —
    same approach_signature."""
    original = (
        f"--- a/{DEST}/repair_target.py\n"
        f"+++ b/{DEST}/repair_target.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def handler():\n"
        "-    from acme.old.money import Money\n"
        "+    from acme.common.money import Money\n"
        "     return Money()\n"
    )
    retyped = (
        f"--- a/{DEST}/repair_target.py\n"
        f"+++ b/{DEST}/repair_target.py\n"
        "@@ -1,4 +1,4 @@\n"
        "+\n"
        " def handler():\n"
        "-    from acme.old.money import Money\n"
        "+    from   acme.common.money   import Money\n"
        "     return Money()\n"
    )
    assert _sig([original]) == _sig([retyped])


def test_a_genuinely_different_fix_does_not_collide() -> None:
    original = (
        f"--- a/{DEST}/repair_target.py\n"
        f"+++ b/{DEST}/repair_target.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def handler():\n"
        "-    from acme.old.money import Money\n"
        "+    from acme.common.money import Money\n"
        "     return Money()\n"
    )
    different = (
        f"--- a/{DEST}/repair_target.py\n"
        f"+++ b/{DEST}/repair_target.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def handler():\n"
        "-    return None\n"
        "+    return compute()\n"
    )
    assert _sig([original]) != _sig([different])


def test_the_signature_is_deterministic_across_repeated_calls() -> None:
    """`--llm-cache read-only` reproduces every signature byte-identically (§12.36): the pure
    half of that guarantee is that this function is a deterministic pure computation."""
    diff = (
        f"--- a/{DEST}/x.py\n+++ b/{DEST}/x.py\n@@ -1,2 +1,2 @@\n"
        " a\n-b\n+c\n"
    )
    sigs = {_sig([diff]) for _ in range(5)}
    assert len(sigs) == 1


def test_hunk_order_and_file_order_do_not_matter() -> None:
    a = f"--- a/{DEST}/a.py\n+++ b/{DEST}/a.py\n@@ -1,2 +1,2 @@\n a\n-x\n+y\n"
    b = f"--- a/{DEST}/b.py\n+++ b/{DEST}/b.py\n@@ -1,2 +1,2 @@\n a\n-p\n+q\n"
    assert _sig([a, b]) == _sig([b, a])


def test_a_whitespace_only_hunk_is_discarded_entirely() -> None:
    """docs/SPEC.md:900: "Discard hunks whose change is whitespace-only." — a diff that is PURELY
    a reformatting produces the identical signature as no diff at all for that file."""
    reformat_only = (
        f"--- a/{DEST}/x.py\n+++ b/{DEST}/x.py\n@@ -1,2 +1,2 @@\n"
        " a\n-  b\n+b\n"  # same token stream, different leading whitespace
    )
    real_change = (
        f"--- a/{DEST}/y.py\n+++ b/{DEST}/y.py\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n"
    )
    # A diff with ONLY a whitespace-only hunk contributes nothing: combined with a real change to
    # a SECOND file, its signature must equal the real change alone.
    assert _sig([reformat_only, real_change]) == _sig([real_change])


def test_a_file_add_and_a_file_delete_are_distinct_kinds() -> None:
    added = f"--- /dev/null\n+++ b/{DEST}/new.py\n@@ -0,0 +1,1 @@\n+x\n"
    deleted = f"--- a/{DEST}/new.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-x\n"
    assert _sig([added]) != _sig([deleted])


# =======================================================================================
# 2. Rule 12 mutation proof (manual — mirrors `tests/test_d89_phase2_claim_lifecycle.py`'s own
#    precedent for a happy-path mutation, and CLAUDE.md's "pick the discriminating mutation").
# =======================================================================================
#
# `test_a_reindented_reordered_offset_shifted_repeat_collides_with_the_original` above is the
# claim under test: `approach_signature` must be insensitive to hunk line offsets and formatting.
# The discriminating mutation is the one CLAUDE.md names directly: "a mutation that makes the
# signature sensitive to whitespace must redden the collision assertion specifically."
#
# Performed by hand against `src/fleet/rewrite/approach.py` (reverted before this file was
# committed; the file is untracked/new, so verified against a pre-mutation BACKUP COPY per
# CLAUDE.md's "diff against a backup, not HEAD" — a HEAD-relative gate is meaningless for a file
# `git diff` has never seen):
#
#   - `_is_whitespace_only(hunk)`'s body changed from `return "".join(pre).split() ==
#     "".join(post).split()` to `return False` (never discard a whitespace-only hunk), AND
#     `compute_approach_signature`'s hash input gained `joined += "".join(diffs)` — the raw diff
#     text (offsets, whitespace, everything) now reaches the digest alongside the normalized
#     elements.
#   - `git diff --numstat --no-index <backup> src/fleet/rewrite/approach.py` reported `2  2`
#     (2 lines changed, non-empty — the mutation genuinely changed the code; CLAUDE.md's
#     zero-change gate).
#   - Result: 3 of 6 tests failed —
#     `test_a_reindented_reordered_offset_shifted_repeat_collides_with_the_original` (the
#     DISCRIMINATING case this mutation exists to catch), plus
#     `test_hunk_order_and_file_order_do_not_matter` and
#     `test_a_whitespace_only_hunk_is_discarded_entirely` (both also assert an
#     offset/whitespace/order-insensitivity the mutation directly breaks, so their reddening is
#     expected and not a sign of a destructive/module-outage mutation).
#   - `test_a_genuinely_different_fix_does_not_collide`,
#     `test_the_signature_is_deterministic_across_repeated_calls`, and
#     `test_a_file_add_and_a_file_delete_are_distinct_kinds` still PASSED — an all-fail would be
#     the "unimported or destructive mutation" tell CLAUDE.md warns against; 3/6 passing rules
#     that out, so the mutation is a real, discriminating perturbation of the property under test.
#   - The file was restored from the backup and `diff`-verified byte-identical before this test
#     file was committed.


# =======================================================================================
# 3. Fix round 1 (task review): the ast-grep fallback warns once, never for an unmapped
#    language, and never on the common (no logger) path.
# =======================================================================================
class _RecordingLog:
    """The one method `_warn_ast_grep_fallback` calls — a structlog `BoundLogger` narrowed to
    exactly what this module needs, so no real structlog wiring is required to test it."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def warning(self, event: str, **kw: object) -> None:
        self.calls.append({"event": event, **kw})


def test_ast_grep_fallback_warns_once_and_never_for_an_unmapped_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reviewed gap (fix round 1): a broken/absent `ast-grep` used to degrade classification
    silently, forever, with nothing to notice it by. `_warn_ast_grep_fallback` fires exactly once
    per process on a GENUINE ast-grep failure and never for a language simply absent from the
    kind maps, which is a disclosed, by-design gap rather than a degradation.
    """
    import fleet.rewrite.approach as approach_mod

    monkeypatch.setattr(approach_mod, "_warned_ast_grep_fallback", False)

    async def _missing_binary(
        argv: object, *, cwd: object = None, env: object = None,
        deadline: object = None, timeout_s: object = None,
    ) -> None:
        raise FileNotFoundError("ast-grep")

    import_diff = (
        f"--- a/{DEST}/x.py\n+++ b/{DEST}/x.py\n@@ -1,2 +1,2 @@\n"
        " a\n-import old_thing\n+import new_thing\n"
    )
    log = _RecordingLog()
    sig1 = asyncio.run(
        compute_approach_signature([import_diff], runner=_missing_binary, log=log)  # type: ignore[arg-type]
    )
    assert len(log.calls) == 1, "warns on the FIRST ast-grep failure"
    assert log.calls[0]["event"] == "approach_signature_ast_grep_fallback"
    assert log.calls[0]["reason"] == "binary not found on PATH"

    # A second, independent call must NOT warn again — one warning per process.
    sig2 = asyncio.run(
        compute_approach_signature([import_diff], runner=_missing_binary, log=log)  # type: ignore[arg-type]
    )
    assert len(log.calls) == 1, "the second occurrence is silent"
    assert sig1 == sig2, "the fallback classification is still deterministic"

    # An unmapped language (ruby is not in _IMPORT_KINDS/_PACKAGE_KINDS) never even reaches
    # ast-grep, so it must never warn — proven with the REAL default runner, never invoked here.
    monkeypatch.setattr(approach_mod, "_warned_ast_grep_fallback", False)
    ruby_log = _RecordingLog()
    ruby_diff = f"--- a/{DEST}/x.rb\n+++ b/{DEST}/x.rb\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n"
    asyncio.run(compute_approach_signature([ruby_diff], log=ruby_log))  # type: ignore[arg-type]
    assert ruby_log.calls == [], "an unmapped language is a disclosed gap, not a degradation"


def test_ast_grep_fallback_is_silent_when_no_logger_is_supplied() -> None:
    """`log=None` (the default) must never raise — most callers, including every OTHER test in
    this file, have no bound logger to hand."""
    async def _missing_binary(
        argv: object, *, cwd: object = None, env: object = None,
        deadline: object = None, timeout_s: object = None,
    ) -> None:
        raise FileNotFoundError("ast-grep")

    diff = (
        f"--- a/{DEST}/x.py\n+++ b/{DEST}/x.py\n@@ -1,2 +1,2 @@\n"
        " a\n-import old_thing\n+import new_thing\n"
    )
    sig = asyncio.run(compute_approach_signature([diff], runner=_missing_binary))  # type: ignore[arg-type]
    assert len(sig) == 64
