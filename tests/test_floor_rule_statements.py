"""The §11.5-step-5 re-entry-floor rule, bound: four statements of it, and the code they describe.

The rule is stated in four places — `docs/SPEC.md` Constraint 7, `docs/SPEC.md` §11.5 step 5,
`docs/DECISIONS.md` ADR-0076 §1, and the `RESUME_DEMOTE` comment in `src/fleet/models/enums.py`.
Until this file, **nothing bound any of them to each other, or to
`orchestrator/reentry.phase_floor`**.
They agreed because a lane made them agree by hand in `f466287`, and the round that produced them
produced five successive wrong-successor corrections — a fix shipping a narrower or differently
wrong version of the claim it was correcting — so hand agreement is exactly the state that has
already failed here.

Three layers, because the four statements are not the same kind of artifact and pretending they are
would be the "convention wearing a mechanism's clothes" CLAUDE.md Rule 12 forbids:

**Layer A — identity, for the three prose copies.** They are byte-identical once whitespace is
normalised (336 characters, measured). A census locates every site that states the quantifier, then
each census site must carry the canonical clause *anchored at that offset*; a site that states the
rule in its own words is named by file and line. Whitespace is normalised away first, so a reflow
or re-indent passes and only a change of *words* fails — the shape `6e0a5fa` established for the
ADR-0075 `effort` annotation, whose reflow control is what made it survivable.

**Layer B — the text drives the assertion, so the docs are bound to the code and not merely to each
other.** The status names the clause calls hard stops are *parsed out of the clause* and compared
to `reentry._HARD_STOPS`; the symbol the clause cites is *parsed out* and resolved on the module;
the fallback phase the clause names is *parsed out* and fed to `phase_floor`. Nobody re-authors an
assertion when the prose changes: editing the prose changes what is asserted, and a false edit
fails.

**Layer C — vocabulary, for the code comment.** `enums.py`'s statement is a paraphrase, correctly:
it is a code comment in a different register, and demanding textual identity of it would be wrong.
So it is bound by what it must *name* — a whitelist of the load-bearing distinctions, plus the
same parsed-out hard-stop set as Layer B. This is genuinely weaker than Layer A and the weakness is
stated rather than implied: see `test_the_enums_paraphrase_names_every_load_bearing_distinction`.

What this file does **not** bind is recorded in that test's docstring and in the module-level
`_RESIDUAL` note below, so that no future reader mistakes three layers for closure.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from fleet.models.enums import Phase, RepoStatus
from fleet.orchestrator import reentry
from fleet.orchestrator.reentry import phase_floor
from fleet.state.repository import PhaseRow

_RESIDUAL = """Not bound, stated rather than implied:

1. A consistent rewrite of all three prose copies to one *false* sentence passes Layer A (they
   still agree) and passes Layer C. Only Layer B's parsed-out claims — the hard-stop status set,
   the cited symbol, the fallback phase — are checked against the code, so a falsehood outside
   those three claims is not caught by anything here.
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
"""

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = _ROOT / "docs" / "SPEC.md"
_DECISIONS = _ROOT / "docs" / "DECISIONS.md"
_ENUMS = _ROOT / "src" / "fleet" / "models" / "enums.py"

# The census anchor: the quantifier phrase, which every prose statement of the rule carries and
# which the pre-`60d400b` ("earliest") and pre-`f466287` (`SCAN` fallback ignoring `_HARD_STOPS`)
# wordings also carried. Anchoring the census on text older than the correction is deliberate: a
# site reverted to either wrong version is still *found*, and then fails the clause check by name.
_CENSUS = re.compile(r"the phase \*\*above\*\* the \*\*highest\*\* phase below")

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
    r"the phase \*\*above\*\* the \*\*highest\*\* phase below[\s\S]{0,600}?there is no such phase"
)

# How many prose statements exist, per file. A deleted or added copy fails here first.
_EXPECTED_SITES = {"docs/SPEC.md": 2, "docs/DECISIONS.md": 1}


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
    for path in (_SPEC, _DECISIONS):
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


def test_the_three_prose_statements_of_the_floor_rule_are_one_statement() -> None:
    """`docs/SPEC.md` Constraint 7, `docs/SPEC.md` §11.5 step 5 and `docs/DECISIONS.md` ADR-0076 §1
    carry the same clause, word for word once whitespace is normalised.

    This is the drift that actually happened, twice, four hours apart: `60d400b`/`e0404b0` applied
    one wording to three sites and `f466287` had to re-apply a corrected one, because ADR-0076 §1
    had omitted the fallback entirely and then all three carried a fallback that ignored
    `_HARD_STOPS`. Nothing detected either divergence; a reader did.

    Normalised, so a reflow passes and only a change of words fails. Anchored per-site, so the
    failure names the file and line that diverged rather than a count.
    """
    statements = _prose_statements()
    assert len(statements) == 3, f"expected 3 prose statements, found {sorted(statements)}"
    distinct = set(statements.values())
    assert len(distinct) == 1, (
        "the three copies of the re-entry-floor rule have drifted apart — this is the "
        "hand-maintained-agreement state that produced five wrong-successor corrections this "
        "round:\n" + "\n".join(f"  {site}: {text}" for site, text in sorted(statements.items()))
    )


# ------------------------------------------------------------------------------------------
# Layer B -- the clause's own words drive the assertions against `phase_floor`
# ------------------------------------------------------------------------------------------


def test_the_hard_stop_statuses_the_clause_names_are_the_ones_the_code_stops_at() -> None:
    """Parsed out of the prose, compared to `reentry._HARD_STOPS`. Nobody re-authors this.

    The clause says the walk also stops above a phase "which is a `DEGRADED`/`SKIPPED` hard stop".
    Those two names are read *from the clause*, so editing the prose to name a different set — or
    to drop one — fails here without anyone touching this file, and adding a status to
    `_HARD_STOPS` without saying so in the prose fails equally.
    """
    clause = next(iter(set(_prose_statements().values())))
    named = {m.group(1) for m in re.finditer(r"`(DEGRADED|SKIPPED|SUCCEEDED|PENDING)`", clause)}
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


def test_a_hard_stop_below_the_frontier_ends_the_walk_before_evidence_is_consulted() -> None:
    """ "tested *before* evidence, so such a row ends the walk whatever holds below it" — the half
    of the clause `f466287` added, and the half `d0de310` disclosed as missing.

    `BUILD` is `DEGRADED` and its evidence does **not** hold, while `SCAN`'s does. If evidence were
    consulted first the floor would descend to `BUILD`; because `_HARD_STOPS` is tested first the
    walk ends immediately and the floor is `VERIFY`. `f466287` measured `VERIFY`; measured again
    here.
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
    named = set(re.findall(r"\b(DEGRADED|SKIPPED|SUCCEEDED|PENDING)\b", paraphrase))
    actual = {status.name for status in reentry._HARD_STOPS}
    assert named == actual, (
        f"the `RESUME_DEMOTE` comment names {sorted(named)} as hard stops; "
        f"`reentry._HARD_STOPS` is {sorted(actual)}"
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


if __name__ == "__main__":  # pragma: no cover - convenience for a scoped manual run
    raise SystemExit(pytest.main([__file__, "-q"]))
