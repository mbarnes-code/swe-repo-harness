"""Merge-time guard: no two headings in `docs/DECISIONS.md` or `docs/INTEGRATION_HONESTY.md`
claim the same ADR or D number.

Why this exists
----------------
Two agent lanes (GitHub Copilot and a Claude Code session) wrote to `main` concurrently and
each independently minted an ADR-0147; one was renumbered to ADR-0150 on merge (see
`docs/DECISIONS.md`'s ADR-0150 entry, "Renumbered on merge"). CLAUDE.md's "Single writer"
rule (dated 2026-09-26, section 3) is the process fix that stops concurrent writers; this test
is the mechanical backstop a merging round can run before landing a PR, so a collision like the
one above fails a check instead of waiting for a human to notice.

Heading conventions (verified against the real files, not assumed)
--------------------------------------------------------------------
`docs/DECISIONS.md` headings: ``## ADR-<n> — <title>`` (e.g. ``## ADR-0150 — The Harmony
tokeniser vocab is a provisioned...``).

`docs/INTEGRATION_HONESTY.md` uses THREE heading forms for one D-entry: ``**D<n> — ``,
``### D<n> — ``, ``## D<n> — ``. Some entries carry TWO of these forms for the SAME number --
e.g. ``D75`` is headed both ``## D75 — corrections to tracked review findings`` (a grouping
title) immediately followed by ``### D75 — CORRECTION RECORDED, no code defect. ...`` (the
entry itself) at `docs/INTEGRATION_HONESTY.md:5211-5213`. That is NOT a duplicate -- it is one
entry, headed twice. Only two DIFFERENT entries both claiming the same number is a duplicate.
Verified live: of 149 D-heading-form matches in the real file (70 ``**``, 8 ``###``, 71 ``##``),
exactly one number (75) has more than one heading-line match, and its two matches are the
adjacent grouping-title/entry-title pair above, not two independent entries -- so the real file
currently has zero true duplicates in either class, which the integration test below asserts.

Why this is mutation-testable (CLAUDE.md Rule 12)
---------------------------------------------------
The detection logic is two small pure functions, `_duplicate_adr_numbers` and
`_duplicate_d_numbers`, each taking the file's text as a plain string. The unit tests below run
them against literal fixture strings under this file's own control, so a mutation to either
function's logic (dropping one of the three D-heading forms, failing to collapse a genuine
multi-form entry, or comparing heading numbers as strings instead of integers) reddens a
fixture-based test whose fixture is built to discriminate exactly that failure mode -- see the
mutation matrix in this task's report. The one integration test at the bottom then runs the same
two functions against the real files on disk.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

# `## ADR-<n> — <title>` -- the only heading form `docs/DECISIONS.md` uses for an ADR.
_ADR_HEADING = re.compile(r"^## ADR-(\d+) — ", re.MULTILINE)

# The three heading forms `docs/INTEGRATION_HONESTY.md` uses for a D-entry. Line-anchored,
# matching CLAUDE.md's own description of the convention ("heads entries three ways"). Note the
# bold form has NO space between `**` and `D` (`**D50 — `), while the two hash forms DO have a
# space before `D` (`### D50 — `, `## D50 — `) -- verified against the real file, not assumed.
_D_HEADING = re.compile(r"^(?:\*\*D|### D|## D)(\d+) — ", re.MULTILINE)


def _duplicate_adr_numbers(text: str) -> list[int]:
    """Every ADR number headed by `## ADR-<n> — ` more than once in `text`, sorted ascending.

    Compares numerically (via `int(...)`), not as raw digit strings, so `ADR-0099` and
    `ADR-099` are recognised as the same number.
    """
    numbers = [int(m.group(1)) for m in _ADR_HEADING.finditer(text)]
    counts = Counter(numbers)
    return sorted(n for n, c in counts.items() if c > 1)


def _duplicate_d_numbers(text: str) -> list[int]:
    """Every D number claimed by more than one DISTINCT entry in `text`, sorted ascending.

    Takes the union of numbers found across all three heading forms, then collapses
    consecutive heading matches that share a number into a single entry -- that is what makes
    a genuine two-form entry (e.g. a `## D75 — ` grouping title immediately followed by its own
    `### D75 — ` entry title, with no other D-heading between them) count once rather than as a
    duplicate. Two matches of the same number that are NOT adjacent in this collapsed sequence
    -- i.e. some other D-heading (of a different number) appears between them -- are two
    different entries claiming the same number, which is the real defect this function exists
    to catch.
    """
    raw_numbers = [int(m.group(1)) for m in _D_HEADING.finditer(text)]
    collapsed: list[int] = []
    for n in raw_numbers:
        if not collapsed or collapsed[-1] != n:
            collapsed.append(n)
    counts = Counter(collapsed)
    return sorted(n for n, c in counts.items() if c > 1)


# ---------------------------------------------------------------------------
# Unit tests against literal, hand-built fixtures (these are what make the logic
# mutation-testable -- see the module docstring and this task's mutation matrix).
# ---------------------------------------------------------------------------


def test_duplicate_adr_numbers_is_empty_for_distinct_headings() -> None:
    fixture = (
        "## ADR-0098 — first decision\n"
        "\nbody text\n\n"
        "## ADR-0099 — second decision\n"
        "\nmore body text\n"
    )
    assert _duplicate_adr_numbers(fixture) == []


def test_duplicate_adr_numbers_finds_two_headings_claiming_the_same_number() -> None:
    fixture = (
        "## ADR-0099 — a decision one lane wrote\n"
        "\nbody text\n\n"
        "## ADR-0099 — a different decision another lane wrote\n"
        "\nmore body text\n"
    )
    assert _duplicate_adr_numbers(fixture) == [99]


def test_duplicate_adr_numbers_compares_numerically_not_as_digit_strings() -> None:
    """`ADR-0099` and `ADR-099` are the same number and must be flagged as a duplicate pair,
    even though their digit strings differ ("0099" != "099")."""
    fixture = "## ADR-0099 — first\n\nbody\n\n## ADR-099 — second\n\nbody\n"
    assert _duplicate_adr_numbers(fixture) == [99]


def test_duplicate_d_numbers_is_empty_for_distinct_entries() -> None:
    fixture = (
        "**D50 — a fixed defect**\n\nbody text\n\n"
        "### D51 — an open defect\n\nbody text\n\n"
        "## D52 — a grouping title\n\n### D52 — the entry under it\n\nbody text\n"
    )
    assert _duplicate_d_numbers(fixture) == []


def test_duplicate_d_numbers_treats_an_adjacent_two_form_entry_as_one_entry_not_a_duplicate() -> (
    None
):
    """The D75 shape in the real file: a `##` grouping title immediately followed by the
    entry's own `###` heading, same number, nothing else in between. Not a duplicate."""
    fixture = (
        "## D75 — corrections to tracked review findings\n"
        "\n"
        "### D75 — CORRECTION RECORDED, no code defect\n"
        "\nbody text explaining the correction\n"
    )
    assert _duplicate_d_numbers(fixture) == []


def test_duplicate_d_numbers_flags_two_different_entries_claiming_the_same_number() -> None:
    """Same number, but a DIFFERENT entry (D60) sits between the two D50 headings -- these are
    two independent claims on D50, exactly the collision this test exists to catch."""
    fixture = (
        "**D50 — first agent's defect entry**\n\nbody text\n\n"
        "**D60 — an unrelated entry**\n\nbody text\n\n"
        "**D50 — second agent's defect entry, same number**\n\nbody text\n"
    )
    assert _duplicate_d_numbers(fixture) == [50]


def test_duplicate_d_numbers_recognizes_a_duplicate_spanning_two_different_forms() -> None:
    """D10 is first headed in the bold form, then -- after an unrelated D20 entry, so this is
    unambiguously two separate entries, not one multi-form entry -- headed again in the
    triple-hash form. A mutation that drops recognition of either form would miss this pair."""
    fixture = (
        "**D10 — first entry, bold form**\n\nbody\n\n"
        "## D20 — an unrelated entry in between\n\nbody\n\n"
        "### D10 — second entry, same number, triple-hash form\n\nbody\n"
    )
    assert _duplicate_d_numbers(fixture) == [10]


def test_duplicate_d_numbers_recognizes_the_bold_form_alone() -> None:
    fixture = "**D77 — first**\n\nbody\n\n**D88 — unrelated**\n\nbody\n\n**D77 — second**\n\nbody\n"
    assert _duplicate_d_numbers(fixture) == [77]


def test_duplicate_d_numbers_recognizes_the_triple_hash_form_alone() -> None:
    fixture = (
        "### D77 — first\n\nbody\n\n### D88 — unrelated\n\nbody\n\n### D77 — second\n\nbody\n"
    )
    assert _duplicate_d_numbers(fixture) == [77]


def test_duplicate_d_numbers_recognizes_the_double_hash_form_alone() -> None:
    fixture = (
        "## D77 — first\n\nbody\n\n## D88 — unrelated\n\nbody\n\n## D77 — second\n\nbody\n"
    )
    assert _duplicate_d_numbers(fixture) == [77]


# ---------------------------------------------------------------------------
# Integration test: the actual merge-time check, against the real files on disk.
# ---------------------------------------------------------------------------


def test_the_real_decisions_and_integration_honesty_docs_have_no_duplicate_numbers() -> None:
    decisions_text = (_REPO_ROOT / "docs" / "DECISIONS.md").read_text(encoding="utf-8")
    honesty_text = (_REPO_ROOT / "docs" / "INTEGRATION_HONESTY.md").read_text(encoding="utf-8")

    dup_adrs = _duplicate_adr_numbers(decisions_text)
    dup_ds = _duplicate_d_numbers(honesty_text)

    assert dup_adrs == [], (
        f"docs/DECISIONS.md has duplicate ADR heading numbers: {dup_adrs}. "
        "Two agent lanes minted the same ADR number -- renumber the later one and disclose "
        "the collision in its entry (see the ADR-0150 entry's own 'Renumbered on merge' note "
        "for the convention)."
    )
    assert dup_ds == [], (
        f"docs/INTEGRATION_HONESTY.md has duplicate D-number heading numbers: {dup_ds}. "
        "Two agent lanes minted the same D number for two different entries -- renumber the "
        "later one."
    )
