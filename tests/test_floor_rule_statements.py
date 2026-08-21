"""The §11.5-step-5 re-entry-floor rule, bound: every statement of it, and the code they describe.

The rule is restated in full in **eight** places — `docs/SPEC.md` Constraint 7, `docs/SPEC.md`
§11.5 step 5, `docs/DECISIONS.md` ADR-0076 §1, the docstring of `orchestrator.reentry.phase_floor`
itself, **`orchestrator/reentry.py`'s module docstring**, the `RESUME_DEMOTE` comment in
`src/fleet/models/enums.py`, and (in the operator register, twice) `src/fleet/cli.py`'s two step-5
refusals — and *summarised* in three more: `fleet resume --help` and its two `docs/SPEC.md`
mirrors. Until this file, **nothing bound any of them to each other, or to
`orchestrator/reentry.phase_floor`**.
(This sentence said "six" while enumerating seven, in the file written to bind quantifiers. The
eighth is the module docstring Layer F now binds; it was the one restatement bound by nothing at
all, seventeen lines above the one `21d6a87` enrolled.)
They agreed because a lane made them agree by hand in `f466287`, and the round that produced them
produced five successive wrong-successor corrections — a fix shipping a narrower or differently
wrong version of the claim it was correcting — so hand agreement is exactly the state that has
already failed here.

Six layers, because these statements are not the same kind of artifact and pretending they are
would be the "convention wearing a mechanism's clothes" CLAUDE.md Rule 12 forbids:

**Layer A — identity, for the four full restatements in the markdown register.** They are
byte-identical once whitespace is normalised (336 characters, measured; `reentry.py`'s docstring
joined them when the wrong quantifier was corrected out of it). A census locates every site that
states the quantifier, then each census site must carry the canonical clause *anchored at that
offset*; a site that states the rule in its own words is named by file and line. Whitespace is
normalised away first, so a reflow or re-indent passes and only a change of *words* fails — the
shape `6e0a5fa` established for the ADR-0075 `effort` annotation, whose reflow control is what made
it survivable.

**Layer B — the text drives the assertion, so the docs are bound to the code and not merely to each
other.** The status names the clause calls hard stops are *parsed out of the clause* and compared
to `reentry._HARD_STOPS`; the symbol the clause cites is *parsed out* and resolved on the module;
the fallback phase the clause names is *parsed out* and fed to `phase_floor`. Nobody re-authors an
assertion when the prose changes: editing the prose changes what is asserted, and a false edit
fails.

**Layer D — the stop condition, on the semantic axis rather than the phrase axis.** Layers A–C all
anchor on the *quantifier phrase*, and a sixth wrong successor came through the gap that leaves: a
sentence seventeen lines below the corrected clause stated the same rule in entirely different
words ("stop at the first phase whose evidence holds, because the phases below it are covered by
it") and was invisible to the census. Wrap-awareness — the technique that caught five other misses
this round — cannot help: normalising whitespace answers "the text is split", never "the claim is
paraphrased". So Layer D drops the quantifier and anchors on the *shape of a stop claim* instead:
any sentence in the five governed files that binds a stop verb to a stop **condition**
("stops/breaks at/on the first ...", "... whose evidence") must also name a hard stop. Its scope
and the two variants measured and rejected are in `_RESIDUAL` item 4.

**Layer C — vocabulary, for the code comment.** `enums.py`'s statement is a paraphrase, correctly:
it is a code comment in a different register, and demanding textual identity of it would be wrong.
So it is bound by what it must *name* — a whitelist of the load-bearing distinctions, plus the
same parsed-out hard-stop set as Layer B. This is genuinely weaker than Layer A and the weakness is
stated rather than implied: see `test_the_enums_paraphrase_names_every_load_bearing_distinction`.

**Layer E — the summary register.** `fleet resume --help` and its two `docs/SPEC.md` mirrors do
not restate the rule: they *name* it, cite §11.5 step 5, and deny the retracted quantifier in one
breath. No clause, so no Layer A; no stop condition, so no Layer D. All nine cases of this module
passed while all three of those sites said "the earliest incomplete phase" — a measured blind
spot, closed by binding the shared phrase, whitelisting the retracted words to their negation, and
exercising that negation against `phase_floor`.

**Layer F — `reentry.py`'s module docstring, the rule's longest statement.** The file that
*implements* the rule states it twice: once in `phase_floor`'s docstring (a Layer A census site
since `21d6a87`) and once, at greater length and in different words, in the module docstring
seventeen lines above. The second was bound by nothing — Layer A cannot see it (no quantifier
phrase) and Layer D considered **0** of its sentences (measured) — so flipping "the walk may not
continue *below* one" to its opposite left all twelve cases green. It is bound as a paraphrase,
like `enums.py`: required phrases, the hard-stop set parsed out of its own words, and the disaster
it names ("demote every repo with an excluded middle phase all the way to `SCAN`") exercised
against `phase_floor`. Widening Layer D to reach it was measured and rejected — see
`_MODULE_RULE_OPENS`.

What this file does **not** bind is recorded in that test's docstring and in the module-level
`_RESIDUAL` note below, so that no future reader mistakes four layers for closure. In particular,
`src/fleet/cli.py`'s two operator-facing restatements are outside the census by register and are
bound in `tests/test_cli.py` instead: `_RESIDUAL` item 6.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fleet.cli import app
from fleet.models.enums import Phase, RepoStatus
from fleet.orchestrator import reentry
from fleet.orchestrator.reentry import phase_floor
from fleet.state.repository import PhaseRow

_RESIDUAL = """Not bound, stated rather than implied:

1. A consistent rewrite of all four full restatements to one *false* sentence passes Layer A
   (they still agree) and passes Layer C. Layer B's parsed-out claims are what reach the code,
   and there are now **four** of them: the hard-stop status set, the cited symbol, the fallback
   phase, and the *order* of the two tests. A falsehood outside those four is caught by nothing
   here.

   This item previously scoped the residual to "a falsehood outside those three claims", which
   put it outside the gap that actually existed — the accepted falsehood was **inside** claim
   one. `(DEGRADED|SKIPPED|SUCCEEDED|PENDING)` was a closed whitelist, so a *superset* clause
   naming `RUNNING` as a hard stop yielded the same two-element set and compared equal; and
   nothing read the ordering word at all, so "tested *after* evidence" — the inverse of what
   `phase_floor` does — read as agreement. Both halves applied to all four sites at once passed
   12/12, measured at `d123035` before either was fixed. Both are closed: `_named_hard_stops`
   reads whatever the statement enumerates rather than what this file expects it to, and
   `_observed_hard_stop_order` measures the code's real order and compares it to the prose's word.
   The same shape one level out is **not** closed: a hard-stop claim made outside the enumeration
   attached to the words "hard stop" ("..., and a `RUNNING` row also ends the walk") is not read.
2. Layer C is a token whitelist. A rewrite of the `enums.py` comment that keeps all seven required
   tokens while stating something false passes. Textual identity is the wrong instrument for a
   paraphrase and there is no honest stronger one short of deleting the paraphrase; the residual is
   the price of keeping it.
3. The prose sites are located by a natural-language anchor, not an injected marker. Re-wording the
   anchor phrase itself makes the census miss a site; the census count assertion turns that into a
   loud failure rather than a silent pass, but it cannot tell "deleted" from "re-worded past the
   anchor". No marker was injected because `docs/SPEC.md`, `docs/DECISIONS.md` and `enums.py` are
   not this lane's files and structural anchoring (the precedent in `tests/test_llm_cache.py`)
   proved sufficient.

4. Layer D is narrower than "every claim about the walk", which is not mechanisable here, and the
   two wider variants were measured before being rejected rather than dismissed. Over the three
   files governed when they were measured -- `reentry.py` and `cli.py` joined later and each adds
   **0** considered sentences, re-measured -- sentence-split on normalised text:

   * **Wide** (walk-subject + any stop verb in the same sentence, hard stop required): flags both
     real defects on the pre-fix tree -- and **6 of the 19** sentences it considers on the
     *correct* tree, every one of them true. Shipping it means a six-entry hand-maintained
     whitelist of correct prose that grows on every future edit and is keyed on sentence text,
     which is the hand-maintained agreement this module exists to end.
   * **Wide, case-insensitive**: worse than wide -- it *passes* `docs/SPEC.md:6969`, the Critical
     defect, because the same sentence contains the ordinary English word "skipped" (in "a repo
     with a `REQUIRES_HUMAN_INTERVENTION` row is skipped entirely"). A detector silent on the
     known-bad state fails Guardrail 6's first check. This is why `_HARD_STOP_NAMED` is
     deliberately **case-sensitive**; adding `re.IGNORECASE` to it re-opens exactly that hole.

   Layer D (narrow) measured: 2 sentences considered, **2 flagged** at `431b02f` (both defects),
   **0 flagged** after the fix. What it still cannot do: it sees only sentences that phrase the
   stop condition in that shape, so a restatement like "the walk ends as soon as a phase's
   evidence is durable" is not considered at all -- the count assertion turns a re-wording past
   the anchor into a loud failure, exactly as the census's does, but it cannot tell "re-worded"
   from "deleted". And "names a hard stop" is not "states the hard-stop rule": a sentence naming
   `DEGRADED` for an unrelated reason passes, which is residual 1 in a new place.

5. Layer E binds a *phrase*, not a claim. The three summary sites name the floor and deny one
   named wrong quantifier; nothing here checks that a summary re-worded into some *third* wrong
   quantifier ("the lowest unsettled phase") is wrong, because the summaries do not restate the
   rule and there is no clause to compare. The count assertion turns such a re-wording into a
   loud failure -- the phrase is gone -- but, as in residual 3, it cannot tell "re-worded" from
   "deleted", and it cannot tell "re-worded correctly" from "re-worded wrongly".

6. Two further sites state the rule in full and are **not** in this census: the operator-facing
   `ResumeIncompleteError` and `UsageError` messages in `src/fleet/cli.py` (`:10083`, `:10401`).
   They restate it in the uppercase, markdown-free register an error message needs (`ABOVE the
   HIGHEST phase below the settled frontier ...`), so `_CENSUS` -- deliberately case-sensitive and
   anchored on markdown emphasis -- cannot see them, and widening it to reach them would make it
   match the register-free prose of every future paraphrase. They are bound instead by
   `tests/test_cli.py:1269` and `:2149`, which assert the phrase in the *rendered* output of the
   two commands that emit it. That is a real binding in another file, not a gap; it is recorded
   here because a reader counting this module's census sites would otherwise conclude the rule is
   stated in five places when it is stated in eight. `cli.py` is in `_GOVERNED`, so Layer D
   *sweeps* both — but swept is not considered: measured, `cli.py` contributes **0** sentences
   that `_STOP_CONDITION` matches, so Layer D asserts nothing about either restatement today. The
   enrolment buys the future case (a re-statement in the stop-condition shape would be
   considered); the earlier wording here said it "does now cover both", which claimed the future
   case as the present one.

7. Layer F binds `reentry.py`'s module docstring the way Layer C binds `enums.py`'s comment, and
   inherits Layer C's residual: a rewrite keeping the three required phrases while asserting
   something false around them passes. Two further gaps, reproduced rather than supposed:

   * **Scope.** Layer F reads numbered item 2 only, because item 1 and the closing paragraph name
     `PENDING` and `REQUIRES_HUMAN_INTERVENTION` for unrelated reasons and would corrupt the
     parsed-out hard-stop set. A false statement of the same rule *elsewhere in that docstring* is
     bound by nothing. Measured: appending "a `SKIPPED` row below the frontier is passed over, not
     treated as an end of the walk" — false against the `_HARD_STOPS` break — leaves all 13 cases
     green, and so does "the floor may be set to a `SKIPPED` phase when nothing below it holds".
     Neither falls in Layer F's span and neither matches `_STOP_CONDITION`. A false sentence that
     *is* in the stop-condition shape is loud, through Layer D's count assertion (measured).
   * **Re-worded vs deleted.** `_MODULE_RULE_OPENS`/`_MODULE_RULE_CLOSES` are natural-language
     anchors, like the census's. Renumbering item 2, or re-wording past them, fails loudly — but
     the failure cannot tell "moved" from "removed". Residual 3 in a new place.

8. The item-1 span added beside Layer F narrows the Scope bullet above but does not close it. It
   binds the *direction* (parsed out, compared to a direction measured from `phase_floor`), the
   predicate item 1 swears off (parsed out, resolved through `reentry.py`'s AST) and the disaster
   it names (parsed out, exercised). It inherits the same "false around the required phrases"
   residual, and it says nothing about the **closing paragraph** — the `REQUIRES_HUMAN_INTERVENTION`
   / nothing-left-unsettled sentence — which remains bound by nothing. Measured on a scratch copy:
   rewriting it so a terminal repo is "demoted to `SCAN`" rather than returning `None` — false
   against the code — leaves every case green.
"""

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = _ROOT / "docs" / "SPEC.md"
_DECISIONS = _ROOT / "docs" / "DECISIONS.md"
_ENUMS = _ROOT / "src" / "fleet" / "models" / "enums.py"
_REENTRY = _ROOT / "src" / "fleet" / "orchestrator" / "reentry.py"
_CLI = _ROOT / "src" / "fleet" / "cli.py"


def _flex(literal: str) -> str:
    """`literal` as a regex whose inter-word gaps match any run of whitespace.

    Layer A's promise is "a reflow passes, a re-word fails", and until `_CENSUS`/`_CLAUSE` were
    built this way the promise was only half true: normalisation is applied to the *matched* text,
    but the anchor and the terminator are matched against the raw file, so a wrap falling inside
    either one made the census miss a site or the clause "not reach its terminator" — a hard
    failure on a purely cosmetic edit. `docs/SPEC.md:1567` already wraps mid-phrase (`never the
    earliest\\nincomplete phase`), which is how the same blind spot was measured on the summary
    sites Layer E covers. Every current match still matches: a single space is a run of one.
    """
    return r"\s+".join(re.escape(word) for word in literal.split())


# The census anchor: the quantifier phrase, which every prose statement of the rule carries and
# which the pre-`60d400b` ("earliest") and pre-`f466287` (`SCAN` fallback ignoring `_HARD_STOPS`)
# wordings also carried. Anchoring the census on text older than the correction is deliberate: a
# site reverted to either wrong version is still *found*, and then fails the clause check by name.
_CENSUS = re.compile(_flex("the phase **above** the **highest** phase below"))

# The canonical clause, as `f466287` wrote it once and applied to all three sites.
#
# The `{0,600}` bound is load-bearing and was measured, not guessed. An unbounded `.*?` under
# `DOTALL` does NOT stop at the site it started in: on the pre-`f466287` text at
# `docs/SPEC.md:182` it ran ~6,770 lines forward and matched the *next* site's terminator, which
# turned a precise per-site failure into a 400 KB one and would let a site straddle its
# neighbour. The three real clauses measure 342/336/336 raw characters this session (anchor
# through terminator, before normalisation), so 600 admits any plausible reflow -- even one
# character per line -- while excluding any reach into another site.
_CLAUSE = re.compile(
    _flex("the phase **above** the **highest** phase below")
    + r"[\s\S]{0,600}?"
    + _flex("there is no such phase")
)

# How many prose statements exist, per file. A deleted or added copy fails here first.
#
# `reentry.py` is here because the module that *implements* the rule stated it wrongly for the
# whole of this defect class: `phase_floor`'s summary line read "the earliest phase this repo must
# re-enter at" — the quantifier `60d400b` retracted everywhere else — while the module docstring
# eighteen lines above described the backward walk correctly. Nothing saw it, because the census
# ran over the two doc files only. Measured on the state that shipped it: `TRANSFORM` is the
# earliest phase below the frontier whose evidence does not hold and `phase_floor` returns
# `VERIFY`; with `TRANSFORM` `DEGRADED` the earliest reading names `SCAN` and it returns `BUILD`.
_CENSUS_FILES: tuple[Path, ...] = (_SPEC, _DECISIONS, _REENTRY)
_EXPECTED_SITES = {
    "docs/SPEC.md": 2,
    "docs/DECISIONS.md": 1,
    "src/fleet/orchestrator/reentry.py": 1,
}


def _normalise(text: str) -> str:
    """Collapse every run of whitespace. A reflow, a re-indent or a re-wrap of the same sentences
    survives this; only a change of words does not."""
    return " ".join(text.split())


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _prose_statements() -> dict[str, str]:
    """Every prose statement of the floor rule, keyed `"<relpath>:<line>"`, normalised.

    Each census hit must carry the canonical clause anchored **at that offset**, so a site that
    states the rule in its own words is reported by file and line rather than as a bare count
    mismatch.
    """
    found: dict[str, str] = {}
    for path in _CENSUS_FILES:
        rel = path.relative_to(_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        hits = list(_CENSUS.finditer(text))
        assert len(hits) == _EXPECTED_SITES[rel], (
            f"{rel} states the re-entry-floor rule at {len(hits)} site(s), expected "
            f"{_EXPECTED_SITES[rel]} (lines found: "
            f"{[_line_of(text, h.start()) for h in hits]}). A copy was added or removed; if that "
            f"was deliberate, update _EXPECTED_SITES in the same change."
        )
        for hit in hits:
            site = f"{rel}:{_line_of(text, hit.start())}"
            clause = _CLAUSE.match(text, hit.start())
            assert clause is not None, (
                f"{site} states the re-entry-floor rule but does not carry the canonical clause "
                f"(it does not reach the words 'there is no such phase'). This is the hand-"
                f"maintained-agreement state `f466287` ended."
            )
            assert "\n\n" not in clause.group(0), (
                f"{site}'s clause match spans a paragraph break, so it has run past its own site"
            )
            found[site] = _normalise(clause.group(0))
    return found


#: The status names a statement attaches to the words "hard stop", however it punctuates them:
#: `` `DEGRADED`/`SKIPPED` hard stop `` in the markdown clause, `DEGRADED/SKIPPED hard stop` in
#: `enums.py`'s uppercase register, `` `DEGRADED` and `SKIPPED` are hard stops `` in `reentry.py`'s
#: module docstring. **Open, not a whitelist of expected names** -- see
#: `test_the_hard_stop_statuses_the_clause_names_are_the_ones_the_code_stops_at` for the measured
#: superset that a closed four-name parse let through with all 12 cases green.
_HARD_STOP_ENUMERATION = re.compile(
    r"((?:`?[A-Z][A-Z_]+`?(?:/|,\s*|\s+and\s+))*`?[A-Z][A-Z_]+`?)\s*(?:is|are)?\s*hard stops?"
)


def _named_hard_stops(text: str, what: str) -> set[str]:
    """Every status name `text` calls a hard stop, read out of `text` rather than expected of it.

    A statement that no longer attaches an enumeration to the words "hard stop" fails loudly here
    instead of yielding an empty set that would compare equal to nothing and pass vacuously.
    """
    enumeration = _HARD_STOP_ENUMERATION.search(text)
    assert enumeration is not None, (
        f"{what} no longer names any status as a hard stop (expected an enumeration immediately "
        f"before the words 'hard stop'); got: {text[:200]!r}"
    )
    return set(re.findall(r"[A-Z][A-Z_]+", enumeration.group(1)))


def _enums_paraphrase() -> str:
    """The `RESUME_DEMOTE` comment's statement of the floor rule, normalised and scoped.

    Scoped to the rule sentence and its immediate elaboration — not the whole comment block. The
    block's later bullets name `SKIPPED` and `DEGRADED` for unrelated reasons (why each is absent
    from `RESUME_DEMOTE`), and including them would corrupt the parsed-out hard-stop set below.
    """
    text = _ENUMS.read_text(encoding="utf-8")
    block = re.search(r"^RESUME_DEMOTE\b.*?^\}(.*?)(?=\n\n)", text, re.DOTALL | re.MULTILINE)
    assert block is not None, "no `RESUME_DEMOTE` assignment with a trailing comment in enums.py"
    comment = _normalise(
        " ".join(
            line.partition("#")[2]
            for line in block.group(1).splitlines()
            if line.lstrip().startswith("#")
        )
    )
    start = comment.find("step 5 demotes")
    end = comment.find("between the two.")
    assert start != -1 and end != -1, (
        "the `RESUME_DEMOTE` comment no longer states the re-entry-floor rule in the expected "
        f"span ('step 5 demotes' .. 'between the two.'); got: {comment[:200]!r}"
    )
    return comment[start : end + len("between the two.")]


# ------------------------------------------------------------------------------------------
# Layer D -- the stop condition, wherever it is stated and in whatever words
# ------------------------------------------------------------------------------------------

#: `reentry.py` and `cli.py` joined the governed set with Layer E. Measured before adding, per
#: Guardrail 6: each contributes **0** additional considered sentences today (2 before, 2 after),
#: so the count assertion below is unchanged and neither file brings a false positive. What they
#: buy is future: `reentry.py`'s module docstring already paraphrases the walk in its own words,
#: and `cli.py:10083`/`:10401` restate the whole rule to an operator in the uppercase register —
#: neither is reachable by the census, so before this the semantic axis was the only axis that
#: could ever see a wrong stop claim in them, and it was not looking at them.
#:
#: **Being swept is not being considered, and Layer D's enrolment of `reentry.py` was reported as
#: closure of a measured blind spot when it was not.** Zero considered sentences means zero
#: assertions: the module docstring's own rule statement is bound by **Layer F**, not by this
#: layer, and Layer F exists because widening `_STOP_CONDITION` to reach it flags correct prose.
_GOVERNED: tuple[Path, ...] = (_SPEC, _DECISIONS, _ENUMS, _REENTRY, _CLI)

#: What the walk is, in any register the four statements use.
_WALK_SUBJECT = re.compile(
    r"backward(?:s)? (?:walk|search|scan)|walk \*?backward|walks? backward|phase_floor|"
    r"the walk\b|search(?:es)? \*?downward",
    re.IGNORECASE,
)

#: A stop *condition*, not a mere mention of stopping. This is the whole difference between a
#: layer with two considered sentences and zero false positives and one with nineteen and six:
#: "the walk stops above the highest" says which rung, "the walk stops at the first phase whose
#: evidence holds" says on what condition, and only the second can be wrong about `_HARD_STOPS`.
_STOP_CONDITION = re.compile(
    r"(?:stops?|stopping|breaks?|ends?|halts?)\b[^.;]{0,60}?\b(?:at|on|upon)\b[^.;]{0,40}?\bfirst\b"
    r"|(?:stops?|stopping|breaks?)\b[^.;]{0,40}?\bwhose evidence\b"
    r"|first phase whose evidence (?:holds|still holds)",
    re.IGNORECASE,
)

#: **Case-sensitive, deliberately, and this is load-bearing.** Lowercase "skipped" is an ordinary
#: English word: `docs/SPEC.md`'s step-5 paragraph contains "a repo with a
#: `REQUIRES_HUMAN_INTERVENTION` row is skipped entirely" in the very sentence that stated the
#: stop condition wrongly, so a case-insensitive version of this pattern is *silent on the defect
#: this layer exists to catch* -- measured, see `_RESIDUAL` item 4. Do not add `re.IGNORECASE`.
_HARD_STOP_NAMED = re.compile(r"_HARD_STOPS|\bDEGRADED\b|\bSKIPPED\b|hard stop")

#: How many sentences state the stop condition at all. Asserted, for the same reason
#: `_EXPECTED_SITES` is: a re-wording past `_STOP_CONDITION` would otherwise reduce this layer to
#: zero considered sentences and pass vacuously. Measured, not guessed.
_EXPECTED_STOP_CONDITION_SENTENCES = 3

#: Normalised text has exactly one space per whitespace run, so a sentence boundary is a
#: terminator followed by that single space. Splitting on `:` and `;` as well as `.` matters:
#: `docs/SPEC.md` §11.5 step 5 is one multi-clause paragraph, and without the `:` split the
#: defective clause shares a segment with three unrelated ones.
_SENTENCE_BREAK = re.compile(r"(?<=[.;:])\s")


def _stop_condition_sentences() -> list[tuple[str, str]]:
    """`[(site, sentence)]` for every sentence in the governed files that says the walk stops
    *on a condition* -- in any words, not only the census's quantifier phrase."""
    out: list[tuple[str, str]] = []
    for path in _GOVERNED:
        rel = path.relative_to(_ROOT).as_posix()
        raw = path.read_text(encoding="utf-8")
        text = _normalise(raw)
        # offset in the normalised text -> offset in the raw text, so a failure names a real line
        offsets: list[int] = []
        previous_was_space = False
        for index, char in enumerate(raw):
            if char.isspace():
                if not previous_was_space:
                    offsets.append(index)
                previous_was_space = True
            else:
                offsets.append(index)
                previous_was_space = False
        starts = [0] + [m.end() for m in _SENTENCE_BREAK.finditer(text)]
        for start, end in zip(starts, [*starts[1:], len(text)], strict=True):
            sentence = text[start:end]
            if _WALK_SUBJECT.search(sentence) and _STOP_CONDITION.search(sentence):
                out.append((f"{rel}:{_line_of(raw, offsets[start])}", sentence.strip()))
    return out


def test_every_sentence_stating_the_walks_stop_condition_names_the_hard_stop() -> None:
    """The layer the census could not be: it anchors on the *shape of the claim*, not the phrase.

    `docs/SPEC.md:6969` and `docs/DECISIONS.md:7333` each stated where `phase_floor`'s backward
    walk stops and omitted the `_HARD_STOPS` break that `reentry.phase_floor` tests *first*. Neither
    shared a word with the corrected clause seventeen lines above, so neither was in the census,
    and no amount of whitespace normalisation would have found them -- normalising answers "the
    text is split", never "the claim is paraphrased". A reconciler implementing the walk from
    either sentence returns `SCAN` where `phase_floor` returns `VERIFY` (BUILD `DEGRADED`) and
    `BUILD` (TRANSFORM `SKIPPED`).

    Measured at `431b02f`: 2 sentences considered, 2 flagged. After the fix: 2 considered, 0
    flagged. The count is asserted first, so a re-wording past `_STOP_CONDITION` fails loudly
    instead of emptying the layer.
    """
    sentences = _stop_condition_sentences()
    assert len(sentences) == _EXPECTED_STOP_CONDITION_SENTENCES, (
        f"{len(sentences)} sentence(s) in {[p.name for p in _GOVERNED]} state where the backward "
        f"walk stops, expected {_EXPECTED_STOP_CONDITION_SENTENCES} "
        f"(found: {[site for site, _ in sentences]}). A statement was added, deleted, or re-worded "
        f"past `_STOP_CONDITION` -- if deliberate, update the constant in the same change."
    )
    silent = [(site, text) for site, text in sentences if not _HARD_STOP_NAMED.search(text)]
    assert not silent, (
        "these sentences say where the backward walk stops without naming the `_HARD_STOPS` break "
        "that `reentry.phase_floor` tests BEFORE evidence -- a reconciler following them rebuilds the "
        "walk that demotes every repo with an excluded middle phase to `SCAN` on every resume:\n"
        + "\n".join(f"  {site}: {text}" for site, text in silent)
    )


def _row(phase: Phase, status: RepoStatus) -> PhaseRow:
    return PhaseRow(
        run_id="run-1",
        repo_id="repo-1",
        phase=phase,
        status=status,
        attempts=0,
        max_attempts=3,
        lease_owner=None,
        lease_fence=0,
        lease_expires_at=None,
        last_error=None,
        updated_at="2026-08-20T00:00:00Z",
    )


# ------------------------------------------------------------------------------------------
# Layer A -- the three prose copies say one thing
# ------------------------------------------------------------------------------------------


def test_the_four_statements_of_the_floor_rule_are_one_statement() -> None:
    """`docs/SPEC.md` Constraint 7, `docs/SPEC.md` §11.5 step 5, `docs/DECISIONS.md` ADR-0076 §1
    and `orchestrator/reentry.phase_floor`'s docstring carry the same clause, word for word once
    whitespace is normalised.

    This is the drift that actually happened, twice, four hours apart: `60d400b`/`e0404b0` applied
    one wording to three sites and `f466287` had to re-apply a corrected one, because ADR-0076 §1
    had omitted the fallback entirely and then all three carried a fallback that ignored
    `_HARD_STOPS`. Nothing detected either divergence; a reader did.

    Normalised, so a reflow passes and only a change of words fails. Anchored per-site, so the
    failure names the file and line that diverged rather than a count.
    """
    statements = _prose_statements()
    assert len(statements) == 4, f"expected 4 statements, found {sorted(statements)}"
    distinct = set(statements.values())
    assert len(distinct) == 1, (
        "the copies of the re-entry-floor rule have drifted apart — this is the "
        "hand-maintained-agreement state that produced five wrong-successor corrections this "
        "round:\n" + "\n".join(f"  {site}: {text}" for site, text in sorted(statements.items()))
    )


# ------------------------------------------------------------------------------------------
# Layer B -- the clause's own words drive the assertions against `phase_floor`
# ------------------------------------------------------------------------------------------


def test_the_hard_stop_statuses_the_clause_names_are_the_ones_the_code_stops_at() -> None:
    """Parsed out of the prose, compared to `reentry._HARD_STOPS`. Nobody re-authors this.

    The clause says the walk also stops above a phase "which is a `DEGRADED`/`SKIPPED` hard stop".
    Those names are read *from the clause*, so editing the prose to name a different set — or
    to drop one, or to add one — fails here without anyone touching this file, and adding a status
    to `_HARD_STOPS` without saying so in the prose fails equally.

    **The enumeration is open, and it was closed until this change — measured.** The parse used to
    be `(DEGRADED|SKIPPED|SUCCEEDED|PENDING)`: a whitelist of four names, so a clause naming a
    *fifth* status as a hard stop yielded the same two-element set and passed. Reproduced at
    `d123035`: rewriting all four census sites to "`DEGRADED`/`SKIPPED`/`RUNNING` hard stop"
    passed all 12 cases of this module while being false against `reentry._HARD_STOPS`. A
    reconciler implementing that prose stops the walk on every `RUNNING` row — every repo whose
    middle phase is mid-flight re-enters at the phase above it. CLAUDE.md Rule 12's "invert an
    enumeration of escapes into a whitelist" applied one level up: read whatever the clause names
    in the enumeration attached to "hard stop", and let the comparison decide.
    """
    clause = next(iter(set(_prose_statements().values())))
    named = _named_hard_stops(clause, "the floor clause")
    actual = {status.name for status in reentry._HARD_STOPS}
    assert named == actual, (
        f"the floor clause names {sorted(named)} as hard stops; `reentry._HARD_STOPS` is "
        f"{sorted(actual)}"
    )


def test_the_symbol_the_clause_cites_exists_on_the_module_it_names() -> None:
    """The clause cites `orchestrator/reentry._HARD_STOPS` by name. A rename that leaves the prose
    behind — the exact failure mode Guardrail 6 records for renames — fails here."""
    clause = next(iter(set(_prose_statements().values())))
    cited = re.search(r"`orchestrator/reentry\.(_?\w+)`", clause)
    assert cited is not None, "the floor clause no longer cites a symbol in `orchestrator/reentry`"
    assert hasattr(reentry, cited.group(1)), (
        f"the floor clause cites `orchestrator/reentry.{cited.group(1)}`, which does not exist"
    )


def test_the_fallback_phase_the_clause_names_is_what_phase_floor_returns() -> None:
    """ "...and `SCAN` only if there is no such phase" — the phase name is parsed out of the clause
    and fed to `phase_floor`, on the state the clause describes: a repo with no rows at all, so no
    phase below the frontier holds and none is a hard stop.

    Measured by `f466287` as `SCAN`; measured again here, from the prose's own word.
    """
    clause = next(iter(set(_prose_statements().values())))
    named = re.search(r"`(\w+)` only if there is no such phase", clause)
    assert named is not None, (
        "the floor clause no longer names a fallback phase in the form "
        "'`<PHASE>` only if there is no such phase'"
    )
    assert phase_floor({}, {}) is Phase[named.group(1)], (
        f"the clause says the floor is `{named.group(1)}` when no phase below the frontier holds "
        f"and none is a hard stop; `phase_floor` returns {phase_floor({}, {})!r}"
    )


def test_the_floor_is_above_the_highest_holder_not_the_earliest_as_all_four_state() -> None:
    """The quantifier every one of the four statements carries, exercised.

    Two phases below the frontier hold. The clause says the floor is the phase **above the
    highest** of them, never that phase itself and never the earliest. `60d400b` corrected exactly
    this word across the prose after every statement had said "earliest" — a reconciler following
    the old prose demotes to `TRANSFORM` here and re-runs a phase whose evidence holds.
    """
    rows = {
        Phase.SCAN: _row(Phase.SCAN, RepoStatus.SUCCEEDED),
        Phase.TRANSFORM: _row(Phase.TRANSFORM, RepoStatus.SUCCEEDED),
        Phase.BUILD: _row(Phase.BUILD, RepoStatus.SUCCEEDED),
        Phase.VERIFY: _row(Phase.VERIFY, RepoStatus.PENDING),
    }
    evidence = {Phase.SCAN: True, Phase.TRANSFORM: True, Phase.BUILD: False}
    floor = phase_floor(rows, evidence)
    assert floor is Phase.BUILD, (
        f"the floor must be the phase ABOVE the highest holder, got {floor!r} — `TRANSFORM` would "
        f"mean the floor landed ON the highest holder, `SCAN` that it took the EARLIEST reading"
    )


#: Which of the clause's two tests it says runs first. Parsed, not expected: the word is the whole
#: behavioural claim, and inverting it is the half of the superset defeat with a real consequence.
_ORDERING = re.compile(r"tested \*(before|after)\* evidence")


def _observed_hard_stop_order() -> str:
    """`"before"` or `"after"`: which test `phase_floor` really applies first, measured.

    Measured on the one state that separates them. `BUILD` is `DEGRADED` and its evidence does not
    hold, `SCAN`'s does. Testing `_HARD_STOPS` first ends the walk at `BUILD` without the floor
    moving onto or below it, so the floor is `VERIFY`. Consulting evidence first moves the floor
    down at least one rung, whichever way the two tests are then arranged -- so every non-`VERIFY`
    result means evidence was consulted at the hard-stop row, which is what "after" asserts.
    """
    rows = {
        Phase.SCAN: _row(Phase.SCAN, RepoStatus.SUCCEEDED),
        Phase.TRANSFORM: _row(Phase.TRANSFORM, RepoStatus.SUCCEEDED),
        Phase.BUILD: _row(Phase.BUILD, RepoStatus.DEGRADED),
        Phase.VERIFY: _row(Phase.VERIFY, RepoStatus.PENDING),
    }
    floor = phase_floor(rows, {Phase.SCAN: True, Phase.BUILD: False})
    return "before" if floor is Phase.VERIFY else "after"


def test_a_hard_stop_below_the_frontier_ends_the_walk_before_evidence_is_consulted() -> None:
    """ "tested *before* evidence, so such a row ends the walk whatever holds below it" — the half
    of the clause `f466287` added, and the half `d0de310` disclosed as missing.

    `BUILD` is `DEGRADED` and its evidence does **not** hold, while `SCAN`'s does. If evidence were
    consulted first the floor would descend to `BUILD`; because `_HARD_STOPS` is tested first the
    walk ends immediately and the floor is `VERIFY`. `f466287` measured `VERIFY`; measured again
    here.

    **The clause's own ordering word drives the second assertion.** Until this change nothing read
    it: the census, `_CLAUSE`, `_HARD_STOP_NAMED` and all four Layer-B/C parses are indifferent to
    `before` vs `after`, so rewriting all four sites to "tested *after* evidence" — false against
    the `_HARD_STOPS` break, which really does precede the evidence test — passed all 12 cases
    (measured at `d123035`). A reconciler implementing that prose moves the evidence test first;
    for a repo whose `BUILD` row is `DEGRADED` and whose `BUILD` evidence does not hold, the floor
    then lands below a `DEGRADED` phase, the one outcome ADR-0077 §5 forbids.
    """
    rows = {
        Phase.SCAN: _row(Phase.SCAN, RepoStatus.SUCCEEDED),
        Phase.TRANSFORM: _row(Phase.TRANSFORM, RepoStatus.SUCCEEDED),
        Phase.BUILD: _row(Phase.BUILD, RepoStatus.DEGRADED),
        Phase.VERIFY: _row(Phase.VERIFY, RepoStatus.PENDING),
    }
    floor = phase_floor(rows, {Phase.SCAN: True, Phase.BUILD: False})
    assert floor is Phase.VERIFY, (
        f"a `DEGRADED` row below the frontier must end the walk above itself; got {floor!r} — "
        f"`BUILD` would mean the floor landed ON the hard stop, `SCAN` that the walk passed it and "
        f"reached the fallback, i.e. that evidence was consulted first"
    )
    stated = _ORDERING.search(next(iter(set(_prose_statements().values()))))
    assert stated is not None, (
        "the floor clause no longer says when the hard-stop test runs relative to the evidence "
        "test (expected 'tested *before* evidence' or 'tested *after* evidence')"
    )
    assert stated.group(1) == _observed_hard_stop_order(), (
        f"the floor clause says the `_HARD_STOPS` test is applied *{stated.group(1)}* evidence; "
        f"`phase_floor` applies it *{_observed_hard_stop_order()}*. Whichever is wrong, a "
        f"reconciler implementing the prose changes which rung the floor lands on."
    )


# ------------------------------------------------------------------------------------------
# Layer C -- the code comment's paraphrase, bound by vocabulary and by the parsed-out set
# ------------------------------------------------------------------------------------------

# Each entry is a load-bearing distinction the paraphrase must name, with the token(s) that
# express it. A whitelist, not a blacklist of wrong words: both the prose copies and the
# paraphrase legitimately contain "earliest" (in "NOT the EARLIEST holder"), so forbidding the
# wrong word is not available — CLAUDE.md Rule 12's "invert an enumeration of escapes into a
# whitelist" applies in the same direction here.
_REQUIRED_IN_PARAPHRASE: dict[str, tuple[str, ...]] = {
    "the floor is above the stopping phase, not on it": ("above",),
    "the quantifier is the highest holder": ("highest",),
    "DEGRADED is a hard stop": ("degraded",),
    "SKIPPED is a hard stop": ("skipped",),
    "both are hard stops, named as such": ("hard stop",),
    "the fallback phase": ("scan",),
    "the function this describes": ("phase_floor",),
}


def test_the_enums_paraphrase_names_every_load_bearing_distinction() -> None:
    """`src/fleet/models/enums.py`'s statement is a paraphrase, and is bound as one.

    **What this binds and what it cannot, stated so the mechanism is not mistaken for closure.**
    It binds: that the comment still states the rule at all (the span assertion in
    `_enums_paraphrase`); that it names each distinction the three prose copies turn on; and that
    the hard-stop statuses it names are exactly `reentry._HARD_STOPS`, parsed out of its own text.
    It cannot bind textual identity with the prose copies — the comment is deliberately in a
    different register, and demanding identity would force a code comment to carry markdown
    emphasis and section citations, which would be a worse comment.

    So a rewrite that keeps all seven tokens while asserting something false passes. That residual
    is real and is recorded in `_RESIDUAL`; it is not closed here, and no convention is offered in
    place of closing it.
    """
    paraphrase = _enums_paraphrase()
    lowered = paraphrase.lower()
    missing = {
        distinction: tokens
        for distinction, tokens in _REQUIRED_IN_PARAPHRASE.items()
        if not any(token in lowered for token in tokens)
    }
    assert not missing, "the `RESUME_DEMOTE` floor-rule comment no longer names: " + "; ".join(
        f"{d} (expected one of {list(t)})" for d, t in sorted(missing.items())
    )
    named = _named_hard_stops(paraphrase, "the `RESUME_DEMOTE` comment")
    actual = {status.name for status in reentry._HARD_STOPS}
    assert named == actual, (
        f"the `RESUME_DEMOTE` comment names {sorted(named)} as hard stops; "
        f"`reentry._HARD_STOPS` is {sorted(actual)}"
    )


# ------------------------------------------------------------------------------------------
# Layer F -- `reentry.py`'s MODULE docstring, the rule's longest statement
# ------------------------------------------------------------------------------------------

#: `21d6a87` enrolled `reentry.py` in `_CENSUS_FILES` with one expected site -- `phase_floor`'s
#: docstring -- and reported the measured blind spot closed. It was not: the **module** docstring
#: seventeen lines above states the same rule at greater length, in its own words, and was bound by
#: nothing. Measured at `d123035`: **0** of its sentences match `_STOP_CONDITION`, so Layer D never
#: considered one; it carries none of `_CENSUS`'s quantifier phrase, so Layer A never saw it.
#: Reproduced before fixing: editing "the walk may not continue *below* one" to "the walk continues
#: *below* one" -- false against the `_HARD_STOPS` break -- left all 12 cases green.
#:
#: **Widening Layer D to reach it was measured and rejected.** The sentence `_STOP_CONDITION` would
#: newly have to consider is "... if the backward walk reaches either, it stops there without
#: moving the floor onto it", and it names no hard stop of its own -- "either" back-references the
#: heading two sentences earlier -- so `_HARD_STOP_NAMED` would flag **correct** prose. A detector
#: that fires on correct prose is worse than the gap (CLAUDE.md Rule 12's stop rule), so this is a
#: Layer C-shaped binding instead: what the statement must NAME, plus claims parsed out of it and
#: fed to the live `phase_floor`.
_MODULE_RULE_OPENS = "2. **`DEGRADED`"
_MODULE_RULE_CLOSES = "the floor onto it."

#: Each entry is a load-bearing half of the module docstring's rule, with the phrase that carries
#: it. Whitespace-flexed, so a reflow of the docstring passes and only a change of words fails --
#: which is the point: "the walk may not continue *below* one" losing its "may not" is the review's
#: reproduced defeat, and it is a change of words.
_REQUIRED_IN_MODULE_RULE: dict[str, str] = {
    "the floor may not land ON an excluded phase": "the floor may not land *on* a `SKIPPED` phase",
    "the walk may not continue BELOW one": "the walk may not continue *below* one",
    "and the walk stops there rather than moving the floor onto it": (
        "it stops there without moving the floor onto it"
    ),
}


def _reentry_module_rule() -> str:
    """`reentry.py`'s module-docstring statement of the hard-stop rule, normalised and scoped.

    Scoped to numbered item 2 -- the hard-stop rule -- because item 1 and the closing paragraph
    name `PENDING` and `REQUIRES_HUMAN_INTERVENTION` for unrelated reasons, and including them
    would corrupt the parsed-out hard-stop set exactly as the `enums.py` bullets would.
    """
    text = _REENTRY.read_text(encoding="utf-8")
    docstring = ast.get_docstring(ast.parse(text))
    assert docstring is not None, "src/fleet/orchestrator/reentry.py has no module docstring"
    normalised = _normalise(docstring)
    start = normalised.find(_MODULE_RULE_OPENS)
    end = normalised.find(_MODULE_RULE_CLOSES, start + 1)
    assert start != -1 and end != -1, (
        "`reentry.py`'s module docstring no longer states the hard-stop rule in the expected span "
        f"({_MODULE_RULE_OPENS!r} .. {_MODULE_RULE_CLOSES!r}). It is the rule's longest statement "
        f"and the file that implements it; got: {normalised[:200]!r}"
    )
    return normalised[start : end + len(_MODULE_RULE_CLOSES)]


def test_the_reentry_module_docstring_states_the_rule_the_code_implements() -> None:
    """The rule's longest statement, bound where it lives — in the file that implements it.

    It says three things `phase_floor` must do, and each is checked rather than trusted: that the
    statuses it calls hard stops are `reentry._HARD_STOPS`, read out of its own words; that it
    still states both directional halves (the floor may not land *on* an excluded phase, and the
    walk may not continue *below* one); and that the disaster it names — "demote every repo with
    an excluded middle phase all the way to `SCAN`" — is one `phase_floor` does not produce, with
    the phase read out of the sentence rather than written here.

    **What this is and is not.** Like Layer C, it is a vocabulary-and-parsed-claims binding, not
    textual identity: this docstring is deliberately in a different register from the canonical
    clause and demanding identity of it would force prose that is worse to read. So a rewrite that
    keeps the three phrases while asserting something false around them passes; that is
    `_RESIDUAL` item 7, and no convention is offered in place of closing it.
    """
    rule = _reentry_module_rule()
    missing = {
        distinction: phrase
        for distinction, phrase in _REQUIRED_IN_MODULE_RULE.items()
        if re.search(_flex(phrase), rule) is None
    }
    assert not missing, (
        "`reentry.py`'s module docstring no longer states: "
        + "; ".join(f"{d} (expected {p!r})" for d, p in sorted(missing.items()))
        + ". Flipping one of these is how a false rule statement reached HEAD with all 12 cases "
        "of this module green."
    )

    named = _named_hard_stops(rule, "`reentry.py`'s module docstring")
    actual = {status.name for status in reentry._HARD_STOPS}
    assert named == actual, (
        f"`reentry.py`'s module docstring names {sorted(named)} as hard stops; "
        f"`reentry._HARD_STOPS` is {sorted(actual)}"
    )

    excluded = re.search(r"`(\w+)` phase is a config exclusion", rule)
    disaster = re.search(r"all the way to `(\w+)`", rule)
    assert excluded is not None and disaster is not None, (
        "`reentry.py`'s module docstring no longer names the excluded status and the phase a walk "
        "that passed over it would reach, so its own failure claim cannot be exercised"
    )
    rows = {
        Phase.SCAN: _row(Phase.SCAN, RepoStatus.SUCCEEDED),
        Phase.TRANSFORM: _row(Phase.TRANSFORM, RepoStatus[excluded.group(1)]),
        Phase.BUILD: _row(Phase.BUILD, RepoStatus.SUCCEEDED),
        Phase.VERIFY: _row(Phase.VERIFY, RepoStatus.PENDING),
    }
    floor = phase_floor(rows, {})
    assert floor is not None and floor is not Phase[disaster.group(1)], (
        f"the module docstring says a walk that passed over a `{excluded.group(1)}` middle phase "
        f"would demote all the way to `{disaster.group(1)}`, and that `phase_floor` does not; with "
        f"`TRANSFORM` `{excluded.group(1)}` and no evidence anywhere it returned {floor!r}"
    )
    assert int(floor) > int(Phase.TRANSFORM), (
        f"the walk passed *below* the excluded `TRANSFORM` phase and landed on {floor!r}; the "
        f"module docstring says it may not continue below one"
    )


#: **A second Layer-F span, over numbered item 1 -- the half that was left unbound, which is the
#: more dangerous half.** The span above covers numbered item **2** only: measured, 950 of the
#: docstring's 2,581 characters. Item 1 and the closing paragraph were bound by nothing -- Layer A
#: cannot see them (no quantifier phrase), Layer D considers **0** of their sentences (measured),
#: and this file's own Layer F span excludes them. Reproduced on a scratch copy before writing
#: this, each mutation confirmed by `git diff` to have changed the text *before* its result was
#: read: "then walks backward from there" -> "walks forward" left every case **green**, as did
#: "never consults `BaseWorker.preconditions_hold`" -> "always consults". A reconciler implementing
#: either rebuilds the ascending scan that promotes a never-cloned repo to the last phase -- the
#: default state of every repo at the start of a run, and the inversion item 1 exists to forbid.
#: Item 2's inversion demotes excluded-middle repos to `SCAN`; item 1's *promotes* fresh repos past
#: work they have never done.
_MODULE_SEARCH_OPENS = "1. **The search runs backward"
_MODULE_SEARCH_CLOSES = "asking only whether `evidence` holds at each earlier phase."

#: Item 1's load-bearing phrases, whitespace-flexed like `_REQUIRED_IN_MODULE_RULE`, so a reflow
#: passes and only a change of words fails. Deliberately NOT including the direction words: those
#: are parsed out below and compared against a direction *measured from `phase_floor` itself*,
#: because a required-phrase list can only ever say "these words are present".
_REQUIRED_IN_MODULE_SEARCH: dict[str, str] = {
    "the walk is anchored on the settled frontier, not on a scan of the ladder": (
        "from the settled frontier"
    ),
    "a forward scan is named as the disaster, not merely as an alternative": (
        "would promote a never-cloned repo"
    ),
}


def _reentry_module_search() -> str:
    """`reentry.py`'s module-docstring statement of the *search direction*, normalised and scoped.

    Scoped to numbered item 1 for the same reason `_reentry_module_rule` is scoped to item 2: the
    two items make different claims and a span over both would mix them.
    """
    text = _REENTRY.read_text(encoding="utf-8")
    docstring = ast.get_docstring(ast.parse(text))
    assert docstring is not None, "src/fleet/orchestrator/reentry.py has no module docstring"
    normalised = _normalise(docstring)
    start = normalised.find(_MODULE_SEARCH_OPENS)
    end = normalised.find(_MODULE_SEARCH_CLOSES, start + 1)
    assert start != -1 and end != -1, (
        "`reentry.py`'s module docstring no longer states the search direction in the expected "
        f"span ({_MODULE_SEARCH_OPENS!r} .. {_MODULE_SEARCH_CLOSES!r}). Item 1 is where the module "
        f"states its central correction; got: {normalised[:200]!r}"
    )
    return normalised[start : end + len(_MODULE_SEARCH_CLOSES)]


def _observed_walk_direction() -> str:
    """`"backward"` or `"forward"`: which way `phase_floor` really moves off the frontier.

    Measured, not written here. `SCAN`/`TRANSFORM` `SUCCEEDED` and `BUILD`/`VERIFY` `PENDING` puts
    the frontier at `BUILD`; with no evidence anywhere, a walk that moves *down* returns a phase
    below it and a scan that moves *up* cannot.
    """
    rows = {
        Phase.SCAN: _row(Phase.SCAN, RepoStatus.SUCCEEDED),
        Phase.TRANSFORM: _row(Phase.TRANSFORM, RepoStatus.SUCCEEDED),
        Phase.BUILD: _row(Phase.BUILD, RepoStatus.PENDING),
        Phase.VERIFY: _row(Phase.VERIFY, RepoStatus.PENDING),
    }
    floor = phase_floor(rows, {})
    assert floor is not None, "phase_floor returned None for an unsettled repo"
    return "backward" if int(floor) < int(Phase.BUILD) else "forward"


def test_the_reentry_module_docstring_states_the_search_direction_the_code_walks() -> None:
    """Numbered item 1 of the module docstring, bound the way item 2 is: parsed, then executed.

    Three claims, none of them written twice. **The direction** is read out of the docstring's own
    words and compared to `_observed_walk_direction()`, so flipping "walks backward from there" to
    "forward" makes the file disagree with the function it describes. **The predicate it swears off**
    is read out of "never consults `X`" and checked against every name `reentry.py` actually
    references, resolved through the AST rather than by substring -- so the sentence stops being
    true the moment someone wires that predicate in, and re-wording it to name a different symbol
    changes what is checked. **The disaster it names** -- promoting a never-cloned repo "straight to
    the last phase" -- is read out and exercised against the live `phase_floor` with no rows at all.

    **What this still cannot catch**, stated rather than implied (see `_RESIDUAL` item 8): a
    consistent rewrite of item 1 that keeps the two required phrases, the direction words and the
    disaster sentence while asserting something false *around* them -- the same residual Layer F
    carries for item 2. It also says nothing about the docstring's closing paragraph, which remains
    bound by nothing.
    """
    span = _reentry_module_search()

    missing = {
        distinction: phrase
        for distinction, phrase in _REQUIRED_IN_MODULE_SEARCH.items()
        if re.search(_flex(phrase), span) is None
    }
    assert not missing, (
        "`reentry.py`'s module docstring no longer states: "
        + "; ".join(f"{d} (expected {p!r})" for d, p in sorted(missing.items()))
    )

    stated = {m.group(1).lower() for m in re.finditer(r"(?:search runs|walks)\s+\**(backward|forward)", span)}
    observed = _observed_walk_direction()
    assert stated == {observed}, (
        f"`reentry.py`'s module docstring says the search {sorted(stated) or ['<nothing>']} off the "
        f"frontier; `phase_floor` measurably walks {observed}. A reconciler implementing the "
        f"docstring builds the ascending scan item 1 exists to forbid, which promotes a "
        f"never-cloned repo past every phase it has not run."
    )

    sworn_off = re.search(r"never consults `([\w.]+)`", span)
    assert sworn_off is not None, (
        "item 1 no longer names the predicate the search refuses to consult, so the refusal cannot "
        "be checked against the code"
    )
    symbol = sworn_off.group(1).rsplit(".", 1)[-1]
    tree = ast.parse(_REENTRY.read_text(encoding="utf-8"))
    referenced = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)} | {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    assert symbol not in referenced, (
        f"`reentry.py`'s module docstring says the search never consults `{symbol}`, and "
        f"`reentry.py` references it. One of the two is wrong and the docstring is what subtask 5 "
        f"and subtask 7's authors read first."
    )

    reach = re.search(r"promote a never-cloned repo straight to the (\w+) phase", span)
    assert reach is not None, (
        "item 1 no longer names where a forward scan would send a never-cloned repo, so its own "
        "failure claim cannot be exercised"
    )
    ladder = list(Phase)
    forbidden = {"last": ladder[-1], "first": ladder[0]}[reach.group(1)]
    floor = phase_floor({}, {})
    assert floor is not forbidden, (
        f"the module docstring says a forward scan would promote a never-cloned repo straight to "
        f"the {reach.group(1)} phase (`{forbidden.name}`), and that `phase_floor` does not; with no "
        f"rows and no evidence it returned {floor!r}"
    )


# ------------------------------------------------------------------------------------------
# Layer G -- the step-5 design plan, the one `plans/` document implementers are routed to
# ------------------------------------------------------------------------------------------

#: **Why one file and not the directory.** `docs/superpowers/plans/design-resume-step5.md` is the
#: document §5 of itself routes subtask authors at, and the only `plans/` file any live task brief
#: names as an authority. It quotes the RETRACTED "earliest phase whose precondition holds" wording
#: three times, attributed to live `docs/SPEC.md` line anchors, and nothing in this module's census
#: reaches `docs/superpowers/plans/` -- so a subtask-4/5/7 author reading it was being handed the
#: sentence `60d400b`/`f466287` removed, as if it were current.
#:
#: **Extending the census to the whole directory was measured and rejected.** The retracted phrase
#: occurs 38 times across 13 files under `docs/` (whole-file whitespace normalisation with an
#: offset-to-line map), and all but these are legitimate history: review reports, ledger entries and
#: `PROGRESS.md` checkpoints quoting the phrase precisely because it was retracted. A directory-wide
#: rule would need a hand-maintained exemption list, which is the instrument CLAUDE.md guardrail 6
#: says rots. So the scope is one file, chosen by a stated reason rather than by convenience, and
#: the rest is disclosed in `_RESIDUAL` item 9 instead of being silently exempted.
_STEP5_PLAN = _ROOT / "docs" / "superpowers" / "plans" / "design-resume-step5.md"

#: The retracted quantifier itself -- text far older than the markers this layer requires, so a
#: reverted marker reads as a failure rather than as a deletion.
_RETRACTED_QUANTIFIER = re.compile(r"earliest\s+phase\s+whose\s+precondition\s+holds", re.IGNORECASE)

#: A supersession marker. **`SUPERSEDED` only, deliberately.** The same file also carries
#: `**SETTLED` markers, but those retire *open questions* (§3's ambiguity list), not retracted
#: quotations -- accepting them here would let a `SETTLED` marker three paragraphs away vouch for an
#: unmarked quote, and would make the orphan direction below fire on every settled ambiguity.
_SUPERSESSION_MARKER = re.compile(r"\*\*SUPERSEDED\b")

#: Markdown heading, any depth -- the section boundary this layer scopes to.
_HEADING = re.compile(r"^#{1,6} .*$", re.MULTILINE)


def _step5_plan_sections() -> list[tuple[int, str]]:
    """`[(first_line_number, section_text)]`, split on markdown headings."""
    text = _STEP5_PLAN.read_text(encoding="utf-8")
    starts = [0] + [m.start() for m in _HEADING.finditer(text)]
    bounds = starts + [len(text)]
    return [
        (text.count("\n", 0, bounds[i]) + 1, text[bounds[i] : bounds[i + 1]])
        for i in range(len(bounds) - 1)
    ]


def test_the_step5_design_plan_marks_its_retracted_spec_quotes_as_superseded() -> None:
    """The retracted quantifier may appear in the step-5 plan -- but never unmarked.

    `docs/superpowers/plans/design-resume-step5.md` is what subtasks 4, 5 and 7 are being built
    from, and it quotes *"demote to the earliest phase whose precondition holds"* against
    `SPEC.md:180-182` and `SPEC.md:6777-6779`. Those quotes were verbatim and correct at the
    document's own declared anchor `7a8bfbb`; `60d400b`/`f466287` then retracted the wording, and
    `docs/SPEC.md` carries no occurrence of it at HEAD. A record of what was true then is history,
    so the quotes stay -- but an author who meets one must meet the retraction beside it, which is
    what this asserts, section by section.

    **Both directions, and neither by count.** A retracted quote in a section with no marker fails
    by `file:line`; a marker in a section with no quote fails the same way, so deleting the quotes
    and leaving the markers is a failure rather than a silent pass.

    **What it cannot catch** (`_RESIDUAL` item 9): it is scoped to one file by a stated reason, so
    the other twelve files carrying the phrase are outside it; it asserts a marker is *present in
    the section*, not that the marker is *true*; and re-wording the retracted phrase itself makes
    every occurrence invisible to it, the same anchor residual the census carries.
    """
    unmarked: list[str] = []
    orphaned: list[str] = []
    quoted = 0
    for first_line, section in _step5_plan_sections():
        hits = list(_RETRACTED_QUANTIFIER.finditer(section))
        marked = _SUPERSESSION_MARKER.search(section) is not None
        quoted += len(hits)
        if hits and not marked:
            unmarked += [
                f"{_STEP5_PLAN.name}:{first_line + section.count(chr(10), 0, m.start())}"
                for m in hits
            ]
        if marked and not hits:
            orphaned.append(f"{_STEP5_PLAN.name}:{first_line}")

    assert quoted, (
        f"{_STEP5_PLAN.name} no longer quotes the retracted quantifier anywhere. If the quotes were "
        "deleted deliberately, delete this layer in the same change -- it is anchored on them."
    )
    assert not unmarked, (
        "these sections of the step-5 design plan quote the RETRACTED 'earliest phase whose "
        "precondition holds' wording with no supersession marker in the same section, and that "
        "document is what subtasks 4, 5 and 7 are built from: " + ", ".join(unmarked)
    )
    assert not orphaned, (
        "these sections carry a supersession marker with nothing retracted left in them -- the "
        "marker outlived what it superseded and now misdirects: " + ", ".join(orphaned)
    )


# ------------------------------------------------------------------------------------------
# Layer E -- the summary register: sites that NAME the floor instead of restating it
# ------------------------------------------------------------------------------------------

#: The shared summary phrase. `fleet resume --help` and its two `docs/SPEC.md` mirrors (§10's verb
#: table, §3.5's un-blocking sentence) deliberately do NOT restate the rule — they name it, cite
#: §11.5 step 5, and disclaim the retracted quantifier in one breath. Layer A's clause is the
#: wrong instrument for a one-line CLI help string, which is why the census never saw them and why
#: all nine of this module's cases passed while all three said "the earliest incomplete phase".
_SUMMARY = "re-entry floor (§11.5 step 5), never the earliest incomplete phase"

#: The retracted quantifier. It may appear in these texts **only** as the negation inside
#: `_SUMMARY` — an enumeration of escapes inverted into a whitelist (CLAUDE.md Rule 12): the word
#: itself cannot be forbidden, because the correct prose is exactly the prose that says it.
_RETRACTED = re.compile(r"earliest incomplete phase")

#: Where the summary is stated, and how often. Sources with zero occurrences are absent, so a copy
#: sprouting in `docs/DECISIONS.md` or `reentry.py` fails as loudly as one going missing.
#:
#: `src/fleet/cli.py` and `fleet resume --help` are the same site counted twice, deliberately: the
#: file entry is the docstring, the help entry is what Typer prints from it. Requiring **both** is
#: the assertion that the one reaches the other — move the summary into a `help=` argument, or let
#: Typer stop rendering the docstring, and the file keeps its 1 while the help drops to 0.
_EXPECTED_SUMMARY_SITES = {
    "fleet resume --help": 1,
    "docs/SPEC.md": 2,
    "src/fleet/cli.py": 1,
}


def _summary_sources() -> dict[str, str]:
    """Every text that could carry a resume re-entry summary, normalised.

    The help entry is the **rendered** `--help`, not `cli.py`'s docstring source: a docstring is
    only a help string if Typer prints it, and the operator reads what Typer prints. The five
    files are read as text so that a summary appearing anywhere in them is counted, wrap or no
    wrap — `docs/SPEC.md:1567` wraps mid-phrase (`never the` / `earliest incomplete phase`), so a
    line-oriented probe finds one SPEC site where there are two, which is how this was mis-measured
    before.
    """
    result = CliRunner().invoke(app, ["resume", "--help"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    sources = {"fleet resume --help": _normalise(result.output)}
    for path in (_SPEC, _DECISIONS, _ENUMS, _REENTRY, _CLI):
        sources[path.relative_to(_ROOT).as_posix()] = _normalise(path.read_text(encoding="utf-8"))
    return sources


def test_the_resume_re_entry_summaries_are_one_phrase_wherever_they_are_stated() -> None:
    """The three summary sites, bound where this rule's binding lives.

    `fleet resume --help` and its two `docs/SPEC.md` mirrors are copies of each other; all three
    once summarised re-entry as "the earliest incomplete phase", the quantifier ADR-0076 §1
    retracted. Measured: this module's nine cases all passed throughout, because Layers A–C anchor
    on the canonical *clause* and Layer D on a stop *condition*, and a summary carries neither.
    Moved here from `tests/test_cli.py`, which pinned them until now — §10's command surface is
    where the *help text* is bound, not where this *rule* is, and a rule bound in two files is the
    hand-maintained agreement this module exists to end.

    The binding is on all three at once and on the shared phrase, for the reason the previous lane
    gave for correcting neither alone: fixing the help line by itself desyncs it from the two SPEC
    lines it copies, and one-edit-at-a-time drift is what produced this defect class.
    """
    counts = {
        source: text.count(_SUMMARY)
        for source, text in _summary_sources().items()
        if _SUMMARY in text
    }
    assert counts == _EXPECTED_SUMMARY_SITES, (
        f"the resume re-entry summary is stated at {counts}, expected "
        f"{_EXPECTED_SUMMARY_SITES} (`fleet resume --help` and the `cli.py` docstring it is "
        "printed from, §10's verb table and §3.5's un-blocking sentence). A copy drifted, was "
        "deleted, or was added."
    )


def test_the_retracted_quantifier_survives_only_as_the_negation_the_summary_makes() -> None:
    """ "Earliest incomplete phase" may be *denied*, never *claimed* — the discriminating half.

    The count assertion above cannot tell "this site was re-worded" from "this site went back to
    the retracted quantifier": both drop the phrase. This one names the difference. Every
    occurrence of the retracted words in any governed text must be immediately preceded by
    `never the `; a site reverting to "continue from each repo's earliest incomplete phase" fails
    here and reports its own surrounding text.
    """
    claimed: list[str] = []
    for source, text in _summary_sources().items():
        for hit in _RETRACTED.finditer(text):
            if text[max(0, hit.start() - 10) : hit.start()] != "never the ":
                claimed.append(f"  {source}: ...{text[max(0, hit.start() - 90) : hit.end()]}...")
    assert not claimed, (
        "these texts state re-entry AS the earliest incomplete phase, the quantifier ADR-0076 §1 "
        "retracted — `phase_floor` stops ABOVE the highest holder below the settled frontier, so "
        "wherever two phases below the frontier hold, the earliest names a rung it never "
        "returns:\n" + "\n".join(claimed)
    )


def test_the_floor_is_not_the_earliest_incomplete_phase_as_the_summaries_deny() -> None:
    """The summaries' negative claim, exercised against `phase_floor` rather than spell-checked.

    "Earliest incomplete phase" is a claim about *status rows*, not about evidence, so it is a
    different quantifier from the one Layer B exercises: it names the settled frontier. On three
    `SUCCEEDED` phases with `VERIFY` `PENDING`, the earliest incomplete phase IS `VERIFY` and the
    floor is `BUILD` — one rung **below** it, in the opposite direction from the "earliest holder"
    error. Both readings are wrong and they are wrong in opposite directions, which is why naming
    only one of them in the prose would leave the summary half-true.
    """
    rows = {
        Phase.SCAN: _row(Phase.SCAN, RepoStatus.SUCCEEDED),
        Phase.TRANSFORM: _row(Phase.TRANSFORM, RepoStatus.SUCCEEDED),
        Phase.BUILD: _row(Phase.BUILD, RepoStatus.SUCCEEDED),
        Phase.VERIFY: _row(Phase.VERIFY, RepoStatus.PENDING),
    }
    earliest_incomplete = next(
        phase for phase in Phase if rows[phase].status is not RepoStatus.SUCCEEDED
    )
    floor = phase_floor(rows, {Phase.SCAN: True, Phase.TRANSFORM: True, Phase.BUILD: False})
    assert earliest_incomplete is Phase.VERIFY  # the state the summaries describe
    assert floor is not earliest_incomplete and floor is Phase.BUILD, (
        f"the summaries say re-entry is the floor and NEVER the earliest incomplete phase "
        f"({earliest_incomplete!r}); `phase_floor` returned {floor!r}"
    )


def test_the_residual_is_recorded_rather_than_implied_closed() -> None:
    """A three-layer binding is not closure, and this file must not read as if it were.

    Guardrail 6's "after fixing an overclaim, re-run the original detector against the fix" applies
    to mechanisms too: the reliable next defect is a narrower successor claiming more than it
    binds. `_RESIDUAL` is the disclosure, and this pins it in place so a later edit that deletes it
    fails rather than silently upgrading the claim.
    """
    assert "Not bound" in _RESIDUAL
    assert _RESIDUAL.count("\n1. ") == 1 and "\n2. " in _RESIDUAL and "\n3. " in _RESIDUAL
    assert "\n4. " in _RESIDUAL, (
        "`_RESIDUAL` item 4 -- Layer D's own scope, and the two wider variants measured and "
        "rejected -- has been deleted. Layer D reads as broader coverage than it has without it."
    )
    assert "\n7. " in _RESIDUAL, (
        "`_RESIDUAL` item 7 -- Layer F's scope, and the two false statements measured green "
        "outside it -- has been deleted. Without it, binding `reentry.py`'s module docstring "
        "reads as binding the whole docstring, which is the narrower-successor shape that "
        "produced this layer in the first place."
    )
    assert "\n5. " in _RESIDUAL and "\n6. " in _RESIDUAL, (
        "`_RESIDUAL` items 5 and 6 -- Layer E binds a phrase and not a claim, and the two "
        "`cli.py` restatements this census still cannot reach -- have been deleted. Without them "
        "the census reads as covering every site that states the rule, and it does not."
    )


if __name__ == "__main__":  # pragma: no cover - convenience for a scoped manual run
    raise SystemExit(pytest.main([__file__, "-q"]))
