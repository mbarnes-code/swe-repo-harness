"""Resolve the `file:line` citations in ``docs/INTEGRATION_HONESTY.md`` against the tree.

Why this module exists
----------------------
The ledger is dense with ``file:line`` citations and, until this file, **nothing in the
suite constructed a path to it** — so no citation in it was ever checked by anything.
Round G paid for that five times: a correction commit wrote 9 of 9 wrong ``src/``
citations (uniformly ``+3``, measured at one commit and landed after another); the review
that caught it had its own boundary falsified by a commit moving ``cli.py`` by ``-19``; a
re-sweep of 63 citations followed; one citation could not be repointed at all because its
target was deleted; and an earlier lane found 13 drifted citations where a brief named 4.

The form, not the values, is what fails
---------------------------------------
A bare ``file:line`` cannot survive an unrelated edit above it. The ledger already carries
a drift-resistant alternative -- a line number **bound to the commit it was measured at**,
written ``(`:7181` at `53e5d8d`)``. That is a claim about a *past* tree: it is correct
forever, it is **exempt** here, and re-pointing it would destroy the record. See
``test_a_commit_bound_citation_is_never_offered_for_resolution``.

What this instrument watches, and why drift cannot leave it unchanged
---------------------------------------------------------------------
For each citation that carries an **anchor** -- a backticked identifier immediately
adjacent to it, as in ``` `LlmConcurrency.for_tier` (`settings.py:227-230`) ``` -- the
watched quantity is:

    whether the cited line range is **contained in** the logical span of that
    identifier's definition in the cited file (see ``_logical_span``).

Drift *is* motion of that span while the cited range stands still, so a rotted citation
takes a contained range to an uncontained one. It cannot leave the quantity unchanged.
Two weaker rules were measured and rejected, and both would have been vacuous greens:

  * "the name occurs as a token inside the cited range" certified
    ``ContainerSandbox.remove()`` (`container.py:202-208`) GREEN against a definition at
    345-358, because the common token ``remove`` appears in that range by coincidence.
    3 of 22 greens were green only that way.
  * "the cited range *intersects* the span" tolerates drift up to the whole extent of a
    definition, so it certified two citations GREEN that had **already drifted inside
    round G**: `WaveScheduler.elapsed_s` (`scheduler.py:423-428`), repointed at
    ``f5a188a`` and since moved to 425-430, and `WaveReport.exit_code`
    (`src/fleet/orchestrator/runner.py:311-320`), since moved to 315-324. Containment
    fails both.

Sensitivity, measured rather than asserted. Of the 14 citations green at ``3dd500d``, the
smallest drift that would redden each is 1 line for six of them, 2 for five, 3 for two,
and 25 for `phase_floor` (`src/fleet/orchestrator/reentry.py:96-103`) -- the one citation
naming the middle of a long definition rather than the definition itself. That last number
is the honest statement of this instrument's blind spot: a citation into the interior of a
definition tolerates drift up to that definition's extent.

Two nearby quantities are useless here and are not what this module keys on: "the file exists" and
"the file still has that many lines" are both **invariant under drift** -- every resolvable
citation is in range and 46 anchored citations are nonetheless unresolved -- and a whole-file
digest would move under any edit at all. That count is **not** hand-maintained: every census
number this module states outside a ``Measured at <sha>:`` record is parsed back out of this
prose and checked against the live survey by
``test_every_census_number_this_module_states_is_the_number_it_derives``. It exists because this
sentence and the census comment below once stated two different counts for the same class, and
the suite could not see it -- nothing read this module's own prose.

One document per profile, and an empty category is not an unchecked one
-----------------------------------------------------------------------
Every check here is quantified over a **category** of the profiled document: its pathed
citations, its anchored citations, its commit-bound citations, its pinned unresolved set.
A document that simply has none of a category turns every check over it into a vacuous
green, and that is not hypothetical.

Measured at 924b159: pointing this module's own survey at each candidate file gives
``docs/DECISIONS.md`` 346 pathed citations and **zero** commit-bound ones, and
``docs/SPEC.md`` zero of either. Reproduced by an independent normalised sweep sharing
none of this module's index, offset-map or extension-whitelist code.
Annotated 2026-08-27 at 9b61878, the commit that appended ADR-0095 to ``docs/DECISIONS.md``:
the same survey now returns **352** for that file. All six added tokens sit *inside* ADR-0095,
which quotes the citations it is about; subtract them by the rule that ADR states of itself in
its §3 -- a pathed token inside ADR-0095 is a quotation of a citation, not a citation the
document makes -- and the figure above stands unchanged at 346. The commit-bound and
``docs/SPEC.md`` halves are untouched. The universe, stated because these figures come in pairs
that differ by it: a *pathed citation* count is a property of the document's text alone, so it
is the same under every universe; only **resolution** depends on one, and there the pair does
differ -- out of tree, 31 under this module's ``_SEARCH_DIRS`` and 29 under
``git ls-tree -r HEAD``, both after that same subtraction (35 and 33 before it). Every figure
here was re-derived at 9b61878 by two probes sharing no normaliser, index or extension list,
which agree on all of them, and each was validated against the prior tree (346 -> 352), an
already-swept file (``docs/SPEC.md``, 0), a synthetic injection (+1) and a cosmetic reflow (0).

So a second profile added naively either reds on day one for the new document, or is
"fixed" by weakening an assertion -- at which point the check stops testing anything for
the file that does carry the category.

A ``DocProfile`` therefore does not merely list a category; it **declares a disposition**
for each, ``assert`` or ``not_applicable`` with a reason, and ``_check_population`` checks
the two in **opposite directions**. ``not_applicable`` is a falsifiable claim that the
category is empty, not a waiver: the moment the category becomes non-empty that
declaration fails and names the reason it has outlived. A ``pytest.skip`` expresses the
same intent and can never fail -- which is exactly the difference between an *empty*
category and an *unchecked* one. See ``test_every_profile_declares_every_assertion_explicitly``,
which reads the categories the test bodies consult out of this module's own AST rather
than from a hand list, and fails **by name** in both directions.

Scope, stated so it is not mistaken for more
--------------------------------------------
Two profiles (see the next section): ``docs/INTEGRATION_HONESTY.md`` and, as of round FF,
``docs/CRITERIA_PLAN.md``. Only citations that name a path; the ledger also holds 343 bare
``:N`` continuation citations whose file comes from surrounding prose, and none of those are
covered. The anchored check runs on ``.py`` targets only (AST); ``.md`` and ``.sql`` citations
get the existence/in-range checks and nothing more.
The prose census below is the **module's**, about the one profile named in
``_CENSUS_PROSE_PROFILE``; a second profile's census would not be stated here and so would
not be checked. That is a disclosed limit of this file's prose, not a mechanism.

A second profile, and a second derivation
------------------------------------------
Round FF adds ``docs/CRITERIA_PLAN.md`` as a second profile -- a file this project edits every
round and that, until now, this module never watched at all. It carries no census-prose sentence
of its own (``_CENSUS_PROSE_PROFILE`` stays ``INTEGRATION_HONESTY``-only; that constant is
*singular by design*, not a list this module happened not to grow yet -- see its own docstring),
and it declares ``commit_bound`` ``not_applicable``: measured 0 matches of ``_COMMIT_BOUND`` at
round FF, the same falsifiable-not-a-waiver shape ``INTEGRATION_HONESTY`` uses for the categories
it lacks.

Round EE's final review found containment alone -- *is the cited range anywhere inside the
anchor's own AST span* -- provably blind to a citation repointed to the WRONG statement inside
that same span: three of that round's own repoints stayed green under it. A wider span offers
more room for that to happen unnoticed, and this module's own containment predicate has no
notion of "how wide is too wide" that would not be the kind of magic numeric slack CLAUDE.md's
Rule 12 forbids. So the fix is not a stricter threshold on the same quantity; it is a SECOND
quantity that does not share containment's blind spot:
``test_a_resolving_anchored_citation_names_its_anchor_at_the_cited_line`` reads the literal
source text AT the cited line(s) -- never the anchor's AST span -- and asks whether the anchor's
own bare identifier is written there. Containment is blind to *which* line inside a wide span is
cited; this check is blind to a citation naming *supporting prose* about a symbol rather than the
symbol's own name (measured, not hypothetical: building this check against
``docs/CRITERIA_PLAN.md`` found exactly one such case, ``RejectedApproach``
(`src/fleet/models/tasks.py:213`), and its pin comment records why it is a correct citation
rather than a defect). That is the disclosed blind spot; it is closed by a pin
(``text_mismatch_pins``), ratcheted in both directions exactly like ``pins`` above, not by
weakening the assertion for every citation.

Scoped, deliberately, to profiles that opt in via ``cross_check_anchors=True`` -- today, only
``CRITERIA_PLAN``. Folding it into ``INTEGRATION_HONESTY`` would run it over that file's
pre-existing anchored-citation population, which this task was told not to sweep; the task-review
measured the cost of that at round FF -- 4 of 20 resolving anchored citations already fail this
predicate (``phase_floor`` at ``docs/INTEGRATION_HONESTY.md:4641``, ``_detail()`` at ``:7289``,
``_AttemptWriter.record`` at ``:7302``, ``_reconcile_tasks_with_git`` at ``:7398``) -- so opting in
today would either surface those four as new failures or require pinning them sight unseen,
neither of which this task does; closing them (drift-vs-supporting-prose needs adjudicating per
site, per the disclosed blind spot above) is sized follow-up work, not a reason to leave the count
unstated. See ``DocProfile.cross_check_anchors``'s own docstring.

The pinned set is not an exemption list
---------------------------------------
``_PINNED_UNRESOLVED`` records every anchored citation that does **not** resolve at the
commit this module landed at. It is a ratchet, not a whitelist: it fails in **both**
directions, by ``file:line``. A new unresolved citation fails
``test_no_unpinned_anchored_citation_fails_to_resolve``; a pinned one that starts resolving
(or whose text changes at all) fails its own
``test_each_pinned_citation_is_still_unresolved`` case. **While a citation is pinned** it
therefore cannot rot silently, which a hand-maintained exemption list can. **That claim now
stops at the retirement exception below, and the exception IS hand-maintained**: a retired
citation leaves the pinned set on an author's judgement rather than by any rule, and is
thereafter covered in one direction only -- it stays in the anchored survey, so it still
fails ``test_no_unpinned_anchored_citation_fails_to_resolve`` if it goes unresolved again,
but a silent repoint of it is caught by nothing here. Stated plainly so the next author is
not misled by the sentence above it: this module is a mechanism up to the pin boundary and a
convention across it.

**One direction of it can nonetheless go green without anything being repaired, and that has
now happened once** (round K, ``_run_transform_wave`` / ``cli.py:4445``, retired from the
tuple below with its measurement):
a pin whose citation points into the *interior* of a definition starts resolving as soon as
unrelated drift in the cited file slides that definition over the cited line -- the citation
text never changed and nothing was fixed. That is not a new blind spot; it is the one the
Sensitivity paragraph above already names, observed. The quantity this ratchet watches is
*containment in the anchor's AST span*, and pure drift in the cited file can move it, so a
pin flipping to green is a prompt to measure, never by itself evidence of a repair.
Its members have **three different causes**
and this module does not claim to separate them -- the ledger marks records in prose, not
in syntax, so no rule here can:
  * genuine drift -- verified independently against history for the ``settings.py``
    cluster: at ``a1178f7`` ``on_exhausted`` was at line 428 and ``clone_timeout_s`` at
    268, exactly as cited, and both moved ``+8`` by ``a9afe9d``;
  * a citation that deliberately names a **usage** site, not the definition (the ledger
    says so of ``response.usage``);
  * a citation inside a passage kept **verbatim as a record**, which must not be repointed.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

# The index is built from these directories ONLY -- never the repo root, recursively or
# otherwise. That is what keeps the verdict identical in a detached worktree and in the
# primary checkout: anything rooted at the repo top sees `.venv`/`bazel-*`/`migration_state.json`
# in one tree and not the other (D85's class -- a sibling instrument read 182 files in a
# worktree and 6,975 in the primary). An earlier draft here also scanned repo-root files;
# it indexed 259 in each tree only by coincidence (the primary carries an untracked
# `migration_state.json`, the worktree carries this module), and **zero** of the 414
# citations resolve to a repo-root file, so the scan bought nothing and was dropped.
# Measured at 3dd500d: 256 files indexed in the worktree holding this module and 255 in
# the primary, which does not yet hold it -- the whole difference is this file. The verdict --
# 414 citations / 409 uniquely resolved / 5 ambiguous / 2 commit-bound / 60 anchored /
# 46 unresolved / 0 unpinned failures / 0 stale pins -- is identical in both.
# `Measured at <sha>:` is what marks that paragraph a RECORD of a past tree rather than a live
# claim, and it is what exempts its numbers -- by that rule, never by a hand-maintained list of
# sites, which is the part that rots -- from
# `test_every_census_number_this_module_states_is_the_number_it_derives`. Annotate it if it
# decays; do not rewrite it. Every census number stated OUTSIDE such a paragraph is a live claim
# and is checked against the survey.
# A citation naming a repo-root file now fails loudly in
# `test_every_pathed_citation_names_a_file_that_exists`, naming the directories searched.
_SEARCH_DIRS = ("src", "tests", "docs")
_EXTS = frozenset(
    {".py", ".md", ".sql", ".toml", ".yaml", ".yml", ".json", ".bzl", ".txt", ".cfg", ".ini", ".sh"}
)

_EXT_ALT = "|".join(sorted(e.lstrip(".") for e in _EXTS))
_CITATION = re.compile(
    r"`(?P<path>[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:" + _EXT_ALT + r"))(?P<lines>(?::\d+(?:-\d+)?)+)`"
)
# The drift-resistant form already in the ledger: a line bound to its commit. Exempt.
_COMMIT_BOUND = re.compile(r"\(`:\d+(?:-\d+)?`\s+at\s+`[0-9a-f]{7,40}`\)")
# An anchor immediately adjacent to a parenthesised citation: `X` (`path:lines`).
_ANCHORED = re.compile(
    r"`(?P<anchor>[^`\n]{3,120})`[,;]?\s*\((?:[^()`]{0,40}\s)?"
    r"`(?P<path>[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:" + _EXT_ALT + r"))(?P<lines>(?::\d+(?:-\d+)?)+)`"
)
_IDENT_PATH = re.compile(r"^\.?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?:\(\))?$")

# A census claim this module makes about itself, in its own prose: a number, at most six
# lowercase words, then one of the three phrasings this class is written in. Deliberately
# vocabulary-bound, and that vocabulary is this check's stated blind spot -- a claim worded
# some other way escapes it.
_CENSUS_CLAIM = re.compile(
    r"\b(?P<n>\d+)\s+(?:[a-z]+\s+){0,6}(?:unresolved|do not resolve|fail to resolve)\b"
)
# The by-rule exemption: `Measured at <sha>:` opens a paragraph recording a PAST tree, whose
# numbers legitimately diverge from today's and must be annotated rather than rewritten.
_RECORD_MARKER = re.compile(r"Measured at [0-9a-f]{7,40}:")


_ASSERT = "assert"
_NOT_APPLICABLE = "not_applicable"

# The category vocabulary. Deliberately a tuple of names and nothing else: what each name
# MEANS lives in the test that consults it, and `test_every_profile_declares_every_assertion_
# explicitly` checks this vocabulary against the set the test bodies actually consult -- read
# out of the AST, in both directions. A hand list of sites is the part that rots.
_CATEGORIES = ("pathed_citations", "anchored", "commit_bound", "pins")
# The accessor whose string-literal argument names a category. Under-deriving here fails
# loudly rather than silently: the covering test compares the AST-derived set against
# `_CATEGORIES` and a missed accessor makes that comparison unequal.
_CATEGORY_ACCESSORS = ("_check_population",)


@dataclass(frozen=True)
class Disposition:
    """What a profile says about one category: checked as present, or claimed empty."""

    kind: str
    reason: str = ""


def _asserted() -> Disposition:
    return Disposition(_ASSERT)


def _not_applicable(reason: str) -> Disposition:
    return Disposition(_NOT_APPLICABLE, reason)


@dataclass(frozen=True)
class DocProfile:
    """One profiled document. Frozen, and dispositions are PAIRS, not a mapping.

    A mapping silently keeps the last of two entries for the same category; a tuple of pairs
    lets `_disposition` see the duplicate and fail. That matters because a duplicate would
    let a `not_applicable` shadow an `assert` with nothing red.
    """

    name: str
    doc_rel: str
    search_dirs: tuple[str, ...]
    pins: tuple[tuple[str, str], ...]
    dispositions: tuple[tuple[str, Disposition], ...]
    # Opt-in to the SECOND, non-sharing derivation (see
    # `test_a_resolving_anchored_citation_names_its_anchor_at_the_cited_line`). Default False and
    # deliberately outside `_CATEGORIES`/`_check_population`: folding it into that governed
    # vocabulary would force every profile to declare a disposition for it, and
    # `INTEGRATION_HONESTY`'s pre-existing anchored citations were measured, at round FF's task
    # review, to contain 4 (of 20 resolving) instances of the class this check would flag --
    # opting it in there would be exactly the "make it blocking retroactively" scope creep this
    # task was told not to do; see the module docstring's Scope section for the four sites.
    cross_check_anchors: bool = False
    # Citations where containment resolves but the cited line does not literally name the
    # anchor -- a deliberate, measured exception (a citation to *supporting prose*, not the
    # definition), pinned the same way `pins` pins a non-resolving citation: both directions
    # checked, never a silent waiver. See `test_a_pinned_text_mismatch_still_mismatches`.
    text_mismatch_pins: tuple[tuple[str, str], ...] = ()


def _disposition(profile: DocProfile, key: str) -> Disposition:
    found = [d for k, d in profile.dispositions if k == key]
    if len(found) != 1:
        raise AssertionError(
            f"profile {profile.name!r} declares {len(found)} dispositions for category "
            f"{key!r}; exactly one is required -- see "
            "test_every_profile_declares_every_assertion_explicitly"
        )
    return found[0]


def _check_population(profile: DocProfile, key: str, size: int) -> None:
    """Assert the declared disposition for ``key``, in whichever direction it declares.

    The quantity watched is **the size of the category in the profiled document**, and the
    defect cannot leave it unchanged: a category that is empty when the profile says
    `assert` is the vacuous-quantification defect itself, and one that is non-empty when the
    profile says `not_applicable` is that claim having rotted. The two directions are
    mutually exclusive, so exactly one of them is true of any tree -- there is no state in
    which both pass, and none in which neither runs.
    """
    disposition = _disposition(profile, key)
    if disposition.kind == _ASSERT:
        assert size, (
            f"profile {profile.name!r} declares category {key!r} as `{_ASSERT}`, but "
            f"{profile.doc_rel} now has none of them -- so every check quantified over that "
            "category passes by vacuous quantification and is testing nothing. Either the "
            "category disappeared from the document, or this profile should declare "
            f"`{_NOT_APPLICABLE}` with a reason."
        )
        return
    if disposition.kind != _NOT_APPLICABLE:
        raise AssertionError(
            f"profile {profile.name!r} declares category {key!r} with unknown disposition "
            f"{disposition.kind!r}; expected {_ASSERT!r} or {_NOT_APPLICABLE!r}"
        )
    assert not size, (
        f"profile {profile.name!r} declares category {key!r} `{_NOT_APPLICABLE}` because "
        f"{disposition.reason!r}, but {profile.doc_rel} now has {size} of them. That "
        "declaration was a claim about the document and it has been falsified: change it to "
        f"`{_ASSERT}` so the checks over this category run."
    )


# Every anchored citation that did not resolve at 3dd500d, as (anchor, citation).
# Both-directions ratchet -- see the module docstring. Do not add to this without a
# measurement; do not delete from it without repointing the citation -- with ONE stated
# exception, measured rather than assumed: a pin may be retired WITHOUT repointing when the
# citation sits inside a passage the ledger rules must not be repointed AND it began
# resolving through drift in the cited file rather than through any edit to the citation.
# Retiring one that way costs real coverage, and WHICH coverage is measured rather than
# guessed: the citation stays in the anchored survey and simply becomes unpinned, so
# `test_no_unpinned_anchored_citation_fails_to_resolve` still fires on it by `file:line` if
# drift ever makes it unresolved again. Exactly one direction is lost, not both -- while
# pinned, `test_each_pinned_citation_is_still_unresolved` ALSO failed if the citation's text
# changed at all, so a silent repoint of a known-wrong citation is what stops being caught.
# So it is recorded at the retirement site and in the ledger entry, never done silently.
# See the round-K retirement noted where `_prepare_repo` / `cli.py:3858` still sits, below.
_PINNED_UNRESOLVED: tuple[tuple[str, str], ...] = (
    (
        "test_every_file_the_generated_files_name_exists_after_the_phase_that_writes_them",
        "tests/test_workers_build.py:1695",
    ),  # L169 defined at [(3301, 3398), (3301, 3398)], cited 1695-1695
    (
        "GoModuleCoordinateError",
        "go.py:92",
    ),  # L787 defined at [(120, 146), (120, 146)], cited 92-92
    ("generate_targets()", "go.py:311"),  # L787 defined at [(364, 368)], cited 311-311
    ("resolution()", "go.py:191"),  # L787 defined at [(297, 363)], cited 191-191
    ("resolution()", "go.py:244"),  # L787 defined at [(297, 363)], cited 244-244
    ("workspace_files", "go.py:188"),  # L787 defined at [(241, 296)], cited 188-188
    (
        "buildverify._bazel_argv",
        "buildverify.py:853",
    ),  # L1441 defined at [(1112, 1147)], cited 853-853
    (
        "test_a_docker_run_that_exits_125_is_not_reported_as_a_broken_build_file",
        "tests/test_workers_build.py:1333",
    ),  # L1970 defined at [(1365, 1448), (1365, 1448)], cited 1333-1333
    ("Git.resolve", "vcs/git.py:265-271"),  # L2022 defined at [(309, 326)], cited 265-271
    (
        ".diagnosis_failure_class",
        "buildverify.py:625-630",
    ),  # L2442 defined at [(636, 638)], cited 625-630
    (
        "BuildWorker._handoff",
        "cli.py:4983-4989",
    ),  # L2459 defined at [(3507, 3547), (5577, 5611), (5811, 5837)], cited 4983-4989
    ("response.usage", "buildverify.py:1158"),  # L2468 defined at [(908, 908)], cited 1158-1158
    (
        "_phase_preflight",
        "cli.py:866-878",
    ),  # L2529 defined at [(936, 950), (936, 950)], cited 866-878
    (
        "apply_and_commit",
        "vcs/commits.py:232-264",
    ),  # L2640 defined at [(261, 295), (261, 295)], cited 232-264
    ("_record", "workers/rewrite.py:535-541"),  # L2645 defined at [(567, 587)], cited 535-541
    (
        "TransformInput",
        "cli.py:3200-3218",
    ),  # L2685 defined at [(3312, 3339), (3312, 3339)], cited 3200-3218
    (
        "max_patch_bytes",
        "cli.py:3219-3224",
    ),  # L2727 defined at [(3332, 3338), (4165, 4165)], cited 3219-3224
    (
        "TransformInput",
        "cli.py:4025",
    ),  # L2728 defined at [(3312, 3339), (3312, 3339)], cited 4025-4025
    (
        "RunContext.llm_policy",
        "orchestrator/context.py:140",
    ),  # L2764 defined at [(188, 188)], cited 140-140
    ("FleetSettings.load", "settings.py:1105"),  # L2870 defined at [(1109, 1187)], cited 1105-1105
    (
        "concurrency.llm.workhorse",
        "settings.py:224",
    ),  # L2934 defined at [(232, 232)], cited 224-224
    (
        "LlmConcurrency.for_tier",
        "settings.py:227-230",
    ),  # L2935 defined at [(234, 238)], cited 227-230
    (".on_exhausted", "settings.py:427-428"),  # L2958 defined at [(436, 436)], cited 427-428
    (".clone_timeout_s", "settings.py:267-268"),  # L2976 defined at [(276, 276)], cited 267-268
    ("llm.max_schema_repairs", "settings.py:680"),  # L3019 defined at [(688, 688)], cited 680-680
    (
        "_transform_payloads",
        "cli.py:4028",
    ),  # L3072 defined at [(4155, 4201), (4155, 4201)], cited 4028-4028
    (
        "cli._TransformEvidence.record()",
        "cli.py:3469-3480",
    ),  # L3169 defined at [(3595, 3611)], cited 3469-3480
    (
        "ContainerSandbox.remove()",
        "container.py:202-208",
    ),  # L3233 defined at [(344, 358)], cited 202-208
    ("reap()", "container.py:239-254"),  # L3234 defined at [(456, 532)], cited 239-254
    (
        "_reap_lines",
        "cli.py:10319",
    ),  # L4731 defined at [(10869, 10919), (10869, 10919)], cited 10319-10319
    (
        "OrchestratorContext.worktree",
        "orchestrator/context.py:206-208",
    ),  # L4771 defined at [(265, 268)], cited 206-208
    (
        "WorktreeManager.reap",
        "sandbox/worktree.py:303-310",
    ),  # L4773 defined at [(343, 379)], cited 303-310
    ("_git_run", "sandbox/worktree.py:134-141"),  # L4780 defined at [(172, 180)], cited 134-141
    (
        "cli._reap_worktree_manager",
        "cli.py:10464-10471",
    ),  # L4781 defined at [(11984, 11992), (11984, 11992)], cited 10464-10471
    (
        "_no_test_may_reach_the_host_docker_daemon",
        "tests/test_cli.py:1609-1617",
    ),  # L4810 defined at [(1914, 1924), (1914, 1924)], cited 1609-1617
    (
        "SqliteSchedulerStore.append_blocked_by",
        "src/fleet/orchestrator/scheduler.py:255",
    ),  # L4994 defined at [(273, 306)], cited 255-255
    (
        "RepoState._stub_invariants",
        "models/state.py:188-193",
    ),  # L5036 defined at [(206, 220)], cited 188-193
    (
        "WaveScheduler.elapsed_s",
        "scheduler.py:423-428",
    ),  # L5856 defined at [(424, 430)], cited 423-428
    (
        "WaveScheduler.breached",
        "scheduler.py:430-437",
    ),  # L5857 defined at [(431, 439)], cited 430-437
    (
        "WaveReport.exit_code",
        "src/fleet/orchestrator/runner.py:311-320",
    ),  # L5865 defined at [(314, 324)], cited 311-320
    # RETIRED in round K under the stated exception above, not repointed. Measured at
    # 506cadb: `_run_transform_wave` (`cli.py:4445`), cited in `D84` step 3 of
    # `docs/INTEGRATION_HONESTY.md`, began RESOLVING -- its definition moved to
    # cli.py:4393-4461, so the cited 4445 now falls in its interior (cli.py:4445 is `ctx,`,
    # an argument inside that function's own body). The flip is datable to this round's last
    # merge, which took cli.py from 13266 to 13413 lines (+147) and the definition from
    # 4246-4314 to 4393-4461; at 12be741 the cited 4445 was still outside it. The
    # citation names a USAGE site -- the call in `_transform_impl`, now at cli.py:4674, which
    # is 229 lines from the cited line -- and it sits in a passage `D84` rules is reported,
    # not repointed. So it is neither repaired nor repointable. It stays an anchored citation
    # of this survey, now resolving and unpinned: still covered by
    # `test_no_unpinned_anchored_citation_fails_to_resolve` if it goes unresolved again, no
    # longer covered against a silent repoint. One direction, not both. (`4393-4461` is the
    # `def` span; `_logical_span` widens it to `4391-4461` over the blank lines above the
    # `def`, and 4445 is interior to both.) The record is the dated marker beside `D84` step 3.
    (
        "_run_transform_wave",
        "cli.py:4445",
    ),  # RE-PINNED 2026-08-30 (round M/N controller). Measured fresh, not inherited: cli.py is
    # now 13830 lines (three round-M merges added ~417 lines total); `_run_transform_wave`'s
    # definition is now at cli.py:4555-4626 (survey span), so the cited `:4445` -- previously
    # interior to the definition per the round-K retirement above -- now falls 110 lines BEFORE
    # it starts, outside any span, and the citation is unresolved again exactly as the
    # retirement comment predicted ("still covered ... if it goes unresolved again"). Per
    # D84's own standing ruling, this citation names a USAGE site (the call inside
    # `_transform_impl`, now at cli.py:4839, 394 lines from the cited line) and sits in a
    # passage that ruling says is reported, not repointed -- so it is re-pinned here rather
    # than repaired, continuing the same non-repointing this citation has had since round K.
    # `docs/INTEGRATION_HONESTY.md`'s `D84` step 3 carries a matching dated marker.
    (
        "_prepare_repo",
        "cli.py:3858",
    ),  # L6242 defined at [(3873, 3978), (3873, 3978)], cited 3858-3858
    (
        "_run_verify_wave",
        "cli.py:9236",
    ),  # round-EE re-pin, corrected a second time (2026-09-02, final review): the prior re-pin's
    #    own `cli.py:9206` was itself wrong -- re-measured directly against source rather than
    #    trusted from the gate (which only checks containment in `_run_verify_wave`'s own span
    #    and cannot tell a wrong-but-outside-the-span citation from a right one). The real call
    #    site inside `_verify_impl` is `cli.py:9236`; `_run_verify_wave`'s own definition starts
    #    at `cli.py:8401`. Re-pinned rather than left to coincidentally resolve, matching D84's
    #    own standing "usage sites are reported, not repointed to resolve" convention above.
    (
        "_wave_snapshot",
        "cli.py:8237-8239",
    ),  # L6330 defined at [(7358, 7380), (7358, 7380)], cited 8237-8239
    (
        "WaveScheduler.breached",
        "scheduler.py:430-437",
    ),  # L6344 defined at [(431, 439)], cited 430-437
    (
        "_ruff()",
        "tests/test_lint_gate.py:63",
    ),  # L6378 defined at [(69, 110), (69, 110)], cited 63-63
)


_INTEGRATION_HONESTY = DocProfile(
    name="INTEGRATION_HONESTY",
    doc_rel="docs/INTEGRATION_HONESTY.md",
    search_dirs=_SEARCH_DIRS,
    pins=_PINNED_UNRESOLVED,
    # All four categories are present in this document and every check over them is live.
    # A profile for a document lacking one declares `_not_applicable("<reason>")` instead,
    # which is checked in the opposite direction -- see `_check_population`.
    dispositions=(
        ("pathed_citations", _asserted()),
        ("anchored", _asserted()),
        ("commit_bound", _asserted()),
        ("pins", _asserted()),
    ),
)

# CRITERIA_PLAN's one anchored-unresolved citation, same both-directions ratchet as
# `_PINNED_UNRESOLVED` above. Re-measured fresh at round FF, not inherited from a prior round's
# sizing research: `record_attempt` (`repository.py:2124-2174`) is genuine drift, verified
# against history exactly like the `settings.py` cluster in `_PINNED_UNRESOLVED` above -- the
# citation was added in `8e16653` (the commit that fixed D90; `git log -S` on the literal
# citation string against `docs/CRITERIA_PLAN.md` names two commits, `8e16653` and the sibling
# ADR/round-Q-plan commit `859ca5b` -- both introduce the same occurrence in unrelated lanes,
# neither is an ancestor of the other, and `record_attempt`'s real definition begins at line
# 2124 at `859ca5b` too), and at that commit
# `record_attempt`'s real (non-stub) definition began at line 2124 (`git show
# 8e16653:src/fleet/state/repository.py | grep -n 'def record_attempt'` -> `2124:`), matching
# the citation exactly. Unrelated growth in `repository.py` since then moved the definition to
# 2269-2337 (survey span), so the citation is not repointable to "the current definition" without
# destroying what it was measured against -- it names the tree as it stood when D90 was
# investigated, the same kind of past-tree claim `INTEGRATION_HONESTY.md` marks
# `Measured at <sha>:` for. `docs/CRITERIA_PLAN.md`'s own prose calls this same paragraph a
# "Dated annotation, 2026-08-31 (round Q whole-branch review)" -- this file's own convention for
# exactly that kind of record.
_PINNED_UNRESOLVED_CRITERIA_PLAN: tuple[tuple[str, str], ...] = (
    (
        "record_attempt",
        "repository.py:2124-2174",
    ),  # L454 defined at [(611, 613), (2269, 2337)], cited 2124-2174
)

# Discovered *while building* this profile, not hypothetical: containment alone (the primary
# predicate) calls this citation resolved, because 213 falls inside `RejectedApproach`'s own
# 208-226 span -- but line 213 is the class's own docstring, not the `class RejectedApproach`
# line, so the second derivation below (which requires the anchor's own token at the cited line)
# flags it. Read against source it is a deliberate, correct citation: the doc's claim is "this
# class has no field that can hold diff text", and line 213 is the docstring sentence stating
# exactly that -- a citation to *supporting prose*, the same category `INTEGRATION_HONESTY.md`'s
# `response.usage` pin documents. Pinned rather than repointed, because there is no more-precise
# line to point it at: the claim is about the class as a whole, not one field.
_TEXT_MISMATCH_PINS_CRITERIA_PLAN: tuple[tuple[str, str], ...] = (
    ("RejectedApproach", "src/fleet/models/tasks.py:213"),
)

_CRITERIA_PLAN = DocProfile(
    name="CRITERIA_PLAN",
    doc_rel="docs/CRITERIA_PLAN.md",
    search_dirs=_SEARCH_DIRS,
    pins=_PINNED_UNRESOLVED_CRITERIA_PLAN,
    # `commit_bound` measured 0 at round FF (`_COMMIT_BOUND` over the file returns no matches) --
    # this file has never yet needed the drift-resistant `(`:N` at `sha`)` form. `not_applicable`
    # is a falsifiable claim, not a waiver: the moment one appears, `_check_population` fails and
    # names this reason as outlived (see the module docstring's "not merely list a category"
    # paragraph).
    dispositions=(
        ("pathed_citations", _asserted()),
        ("anchored", _asserted()),
        ("commit_bound", _not_applicable("measured 0 commit-bound citations at round FF")),
        ("pins", _asserted()),
    ),
    cross_check_anchors=True,
    text_mismatch_pins=_TEXT_MISMATCH_PINS_CRITERIA_PLAN,
)

_PROFILES: tuple[DocProfile, ...] = (_INTEGRATION_HONESTY, _CRITERIA_PLAN)

# The prose census in this module's docstring is about exactly one profile. Named here so
# that adding a second profile cannot silently re-point what the census check asserts.
_CENSUS_PROSE_PROFILE = "INTEGRATION_HONESTY"

# One case per profile for the population check, then one per pin. The leading population
# case is what stops a profile with an empty pin tuple from parametrising into ZERO cases --
# the ratchet's other direction would then be absent rather than empty, and absent is exactly
# the state this module refuses to let a category reach.
_PIN_CASES: tuple[tuple[DocProfile, tuple[str, str] | None], ...] = tuple(
    (p, None) for p in _PROFILES
) + tuple((p, pin) for p in _PROFILES for pin in p.pins)


def _pin_case_id(case: tuple[DocProfile, tuple[str, str] | None]) -> str:
    profile, pin = case
    return f"{profile.name}|<population>" if pin is None else f"{profile.name}|{pin[0]}|{pin[1]}"


@dataclass(frozen=True)
class Citation:
    """One ``path:lines`` citation as it stands in the ledger."""

    doc_rel: str
    doc_line: int
    cited_path: str
    lines_text: str
    resolved: str | None
    ambiguous: tuple[str, ...] = ()

    @property
    def span(self) -> tuple[int, int]:
        nums = [int(n) for n in re.findall(r"\d+", self.lines_text)]
        return min(nums), max(nums)

    @property
    def text(self) -> str:
        return f"{self.cited_path}{self.lines_text}"

    @property
    def site(self) -> str:
        return f"{self.doc_rel}:{self.doc_line}"


@dataclass(frozen=True)
class Anchored:
    """A citation carrying an adjacent identifier anchor, plus its resolution verdict."""

    citation: Citation
    anchor: str
    resolves: bool
    detail: str
    error: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.anchor, self.citation.text)


@dataclass
class Survey:
    root: Path
    profile: DocProfile
    indexed_files: int
    citations: list[Citation] = field(default_factory=list)
    commit_bound: list[int] = field(default_factory=list)
    anchored: list[Anchored] = field(default_factory=list)

    def anchored_by_key(self, key: tuple[str, str]) -> Anchored | None:
        for a in self.anchored:
            if a.key == key:
                return a
        return None


def _build_index(root: Path, search_dirs: tuple[str, ...]) -> tuple[dict[str, set[str]], int]:
    index: dict[str, set[str]] = {}
    count = 0

    def add(rel: str) -> None:
        parts = rel.split("/")
        for i in range(len(parts)):
            index.setdefault("/".join(parts[i:]), set()).add(rel)

    for name in search_dirs:
        base = root / name
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*")):
            if f.is_file() and f.suffix in _EXTS:
                count += 1
                add(f.relative_to(root).as_posix())
    return index, count


def _normalised(text: str) -> tuple[str, list[int]]:
    """Collapse every whitespace run to one space, keeping an offset map back to ``text``.

    A line-oriented sweep certifies a class as fixed when it is not: a citation whose
    anchor sits on the previous physical line is invisible to it. This module therefore
    matches on the normalised string and maps every hit back to a real line number.
    """
    out: list[str] = []
    omap: list[int] = []
    prev_ws = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if not prev_ws:
                out.append(" ")
                omap.append(i)
            prev_ws = True
        else:
            out.append(ch)
            omap.append(i)
            prev_ws = False
    return "".join(out), omap


def _logical_span(node: ast.AST, body: list[str]) -> tuple[int, int]:
    """The lines a careful author would cite for ``node``, widened from the raw AST span.

    Three widenings, each derived from the source rather than from a tolerance constant --
    a bare AST span produces false "drifted" verdicts on citations that are exactly right:
      * **decorators** -- ``lineno`` of a decorated ``def`` is the ``def`` line, so a
        citation that includes ``@property`` starts above the span;
      * **leading blank/comment lines** -- ``docker_run_argv`` (`container.py:127-133`)
        cites the blank line before its ``def`` at 128;
      * **trailing comment lines** -- ``end_lineno`` stops at the closing bracket, so
        ``TERMINAL_STATUSES`` (`models/enums.py:25-28`) cites one line past a span ending
        at 27, and the three lines after it are that statement's own explanatory comment.
    There is no numeric slack anywhere: each edge stops at the first line that is neither
    blank nor a comment.
    """
    start = node.lineno  # type: ignore[attr-defined]
    for dec in getattr(node, "decorator_list", ()):
        start = min(start, dec.lineno)
    end = getattr(node, "end_lineno", None) or start
    while start > 1:
        prev = body[start - 2].strip()
        if prev and not prev.startswith("#"):
            break
        start -= 1
    while end < len(body):
        nxt = body[end].strip()
        if not nxt.startswith("#"):
            break
        end += 1
    return start, end


def _symbol_table(path: Path) -> dict[str, list[tuple[int, int]]]:
    body = path.read_text(encoding="utf-8").split("\n")
    table: dict[str, list[tuple[int, int]]] = {}

    def walk(node: ast.AST, prefix: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = [*prefix, child.name]
                span = _logical_span(child, body)
                table.setdefault(".".join(qual), []).append(span)
                table.setdefault(child.name, []).append(span)
                walk(child, qual)
            elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                span = _logical_span(child, body)
                for tgt in targets:
                    if isinstance(tgt, ast.Name):
                        table.setdefault(".".join([*prefix, tgt.id]), []).append(span)
                        table.setdefault(tgt.id, []).append(span)

    walk(ast.parse("\n".join(body)), [])
    return table


def _anchor_token(anchor: str) -> str:
    """The bare identifier a definition-citation names.

    Same normalisation `build_survey` applies before the symbol-table lookup (strip a trailing
    call-parens marker, a leading attribute dot, then take the last dotted component) -- so this
    check and the containment check start from the same notion of "what the anchor names",
    which is what makes it a genuine cross-check rather than a test of a different anchor.
    """
    key = anchor.rstrip("()").lstrip(".")
    return key.split(".")[-1]


def _cited_source_text(root: Path, resolved: str, lo: int, hi: int) -> str:
    """The exact source lines a citation names, 1-indexed and inclusive -- not the symbol's
    (possibly much wider) enclosing span. This is the quantity the containment check never
    reads: containment asks only whether ``lo, hi`` sit inside the anchor's own AST span."""
    lines = (Path(root) / resolved).read_text(encoding="utf-8").split("\n")
    return "\n".join(lines[lo - 1 : hi])


def build_survey(root: Path, profile: DocProfile) -> Survey:
    """Read one profiled document and resolve every citation in it. All I/O is here.

    Per-citation failures are **captured**, never raised: a single unresolvable citation
    must cost one check, not the whole module. That is the property measured in
    ``test_a_single_unresolvable_citation_costs_one_check_not_the_module``.
    """
    ledger = root / profile.doc_rel
    text = ledger.read_text(encoding="utf-8")
    norm, omap = _normalised(text)
    index, indexed = _build_index(root, profile.search_dirs)
    survey = Survey(root=root, profile=profile, indexed_files=indexed)

    def doc_line(npos: int) -> int:
        return text.count("\n", 0, omap[npos]) + 1

    for m in _COMMIT_BOUND.finditer(norm):
        survey.commit_bound.append(doc_line(m.start()))

    def cite_of(m: re.Match[str]) -> Citation:
        cited = m.group("path")
        hits = sorted(index.get(cited, ()))
        return Citation(
            doc_rel=profile.doc_rel,
            doc_line=doc_line(m.start()),
            cited_path=cited,
            lines_text=m.group("lines"),
            resolved=hits[0] if len(hits) == 1 else None,
            ambiguous=tuple(hits) if len(hits) > 1 else (),
        )

    for m in _CITATION.finditer(norm):
        survey.citations.append(cite_of(m))

    tables: dict[str, dict[str, list[tuple[int, int]]]] = {}
    for m in _ANCHORED.finditer(norm):
        anchor = m.group("anchor").strip()
        cite = cite_of(m)
        if cite.resolved is None or not cite.resolved.endswith(".py"):
            continue
        if not _IDENT_PATH.match(anchor):
            continue  # not an identifier -- prose or a quoted expression, not our class
        key = anchor.rstrip("()").lstrip(".")
        try:
            if cite.resolved not in tables:
                tables[cite.resolved] = _symbol_table(root / cite.resolved)
            table = tables[cite.resolved]
        except (OSError, SyntaxError, ValueError) as exc:
            survey.anchored.append(
                Anchored(cite, anchor, False, "", error=f"{type(exc).__name__}: {exc}")
            )
            continue
        parts = key.split(".")
        spans: list[tuple[int, int]] | None = None
        for i in range(len(parts)):
            spans = table.get(".".join(parts[i:]))
            if spans:
                break
        if not spans:
            continue  # the anchor names nothing this file defines -- not our class
        lo, hi = cite.span
        contained = any(s <= lo and hi <= e for s, e in spans)
        survey.anchored.append(
            Anchored(cite, anchor, contained, f"defined at {spans[:3]}, cited {lo}-{hi}")
        )
    return survey


@pytest.fixture(scope="session")
def surveys() -> dict[str, Survey]:
    """Resolution happens HERE, not at import and not in a ``parametrize`` argument.

    A locator evaluated inside a ``parametrize`` decorator turns any single-site failure
    into a module-wide **collection** error -- this project measured one non-Python line
    taking 20 passing checks to 0 executed. Everything below parametrizes over module
    literals only: profiles and pins, never a survey.

    Keyed by profile name, and ``test_every_profile_declares_every_assertion_explicitly``
    asserts those names are unique -- two profiles sharing a name would collapse into one
    entry here and the second document would go unchecked with nothing red.
    """
    return {p.name: build_survey(_REPO_ROOT, p) for p in _PROFILES}


@pytest.mark.parametrize("profile", _PROFILES, ids=lambda p: p.name)
def test_every_pathed_citation_names_a_file_that_exists(
    profile: DocProfile, surveys: dict[str, Survey]
) -> None:
    survey = surveys[profile.name]
    _check_population(profile, "pathed_citations", len(survey.citations))
    bad = [
        f"{c.site}: `{c.text}` -> no such file under {profile.search_dirs}"
        for c in survey.citations
        if c.resolved is None and not c.ambiguous
    ]
    assert not bad, "citations naming a file that does not exist:\n" + "\n".join(bad)


@pytest.mark.parametrize("profile", _PROFILES, ids=lambda p: p.name)
def test_every_pathed_citation_points_at_a_line_the_file_still_has(
    profile: DocProfile, surveys: dict[str, Survey]
) -> None:
    """Catches the deletion/truncation case -- one round-G citation could not be repointed
    at all because its target had been deleted. Invariant under drift, deliberately: it is
    a different quantity from the anchored check, not a weaker version of it."""
    survey = surveys[profile.name]
    _check_population(profile, "pathed_citations", len(survey.citations))
    bad = []
    for c in survey.citations:
        if c.resolved is None:
            continue
        length = len((survey.root / c.resolved).read_text(encoding="utf-8").split("\n"))
        lo, hi = c.span
        if lo < 1 or hi > length:
            bad.append(f"{c.site}: `{c.text}` -> {c.resolved} has {length} lines")
    assert not bad, "citations pointing past the end of their file:\n" + "\n".join(bad)


@pytest.mark.parametrize("profile", _PROFILES, ids=lambda p: p.name)
def test_no_unpinned_anchored_citation_fails_to_resolve(
    profile: DocProfile, surveys: dict[str, Survey]
) -> None:
    """The drift detector. Fails by ``file:line``, naming the citation and where its
    anchor actually is -- never by count, which tells the next author nothing."""
    survey = surveys[profile.name]
    _check_population(profile, "anchored", len(survey.anchored))
    pinned = set(profile.pins)
    bad = [
        f"{a.citation.site}: `{a.anchor}` (`{a.citation.text}`) -> {a.error or a.detail}"
        for a in survey.anchored
        if not a.resolves and a.key not in pinned
    ]
    assert not bad, (
        "anchored citations that no longer resolve (repoint them, or pin them with a "
        "measurement if the citation names a usage site or sits inside a verbatim "
        "record):\n" + "\n".join(bad)
    )


@pytest.mark.parametrize("case", _PIN_CASES, ids=_pin_case_id)
def test_each_pinned_citation_is_still_unresolved(
    case: tuple[DocProfile, tuple[str, str] | None], surveys: dict[str, Survey]
) -> None:
    """The other direction of the ratchet, one case per pin, plus one population case.

    Parametrised over a module literal -- no locator runs in the decorator -- so a site
    that blows up costs exactly this one case and the module still imports.

    The ``<population>`` case is the one that always exists: a profile whose pin tuple is
    empty parametrises into no pin cases at all, and a check that produced no cases is
    *absent*, which is worse than empty because nothing reports it.
    """
    profile, pin = case
    survey = surveys[profile.name]
    if pin is None:
        _check_population(profile, "pins", sum(1 for a in survey.anchored if not a.resolves))
        return
    found = survey.anchored_by_key(pin)
    assert found is not None, (
        f"pinned citation `{pin[0]}` (`{pin[1]}`) is no longer in {profile.doc_rel} in that "
        f"form. If it was repointed, delete this entry from {profile.name}'s pins."
    )
    if found.error is not None:
        raise AssertionError(f"{found.citation.site}: resolving `{pin[1]}` failed: {found.error}")
    assert not found.resolves, (
        f"{found.citation.site}: `{pin[0]}` (`{pin[1]}`) now resolves ({found.detail}). "
        f"Delete this entry from {profile.name}'s pins."
    )


_CROSS_CHECK_PROFILES = tuple(p for p in _PROFILES if p.cross_check_anchors)


@pytest.mark.parametrize("profile", _CROSS_CHECK_PROFILES, ids=lambda p: p.name)
def test_a_resolving_anchored_citation_names_its_anchor_at_the_cited_line(
    profile: DocProfile, surveys: dict[str, Survey]
) -> None:
    """The SECOND, non-sharing derivation (see the module docstring for why one is needed).

    What quantity this reads, and why containment cannot leave it unchanged: containment asks
    whether ``lo, hi`` fall anywhere inside the anchor's own (possibly multi-hundred-line) AST
    span -- so a citation repointed to the WRONG statement inside that same span stays green,
    which is exactly the failure round EE's final review found in three of that round's own
    repoints. This check instead reads the literal source text AT the cited line(s) and asks
    whether the anchor's own bare identifier appears there. A citation drifted to an unrelated
    statement inside the same enclosing symbol will, in general, not have that symbol's own name
    written on the line it drifted to; a citation still on the definition line, the signature, or
    a usage/call site will. The two checks share no code path: this one never consults
    `_symbol_table` or `_logical_span`.

    Disclosed blind spot, found while building this check rather than asserted in the abstract:
    a citation naming a class/function can legitimately point at a SUPPORTING line inside that
    symbol's own body that does not repeat the symbol's name -- `RejectedApproach`
    (`src/fleet/models/tasks.py:213`) cites the class's own docstring sentence stating the claim
    the doc makes, not the `class RejectedApproach` line itself, and correctly does not contain
    the token `RejectedApproach`. That is not a defect in the citation; it is a defect in
    assuming "names its anchor" is the only way a citation can be right. Such cases are pinned
    exactly like an unresolved anchor is pinned -- `profile.text_mismatch_pins`, both directions
    ratcheted (see `test_a_pinned_text_mismatch_still_mismatches`) -- rather than silently
    exempted or used to weaken this assertion for every citation.

    Scope: only citations that already resolve under containment (an unresolved one is already
    reported by `test_no_unpinned_anchored_citation_fails_to_resolve`, and re-reporting it here
    would tell the next author nothing new). Only profiles that opt in via
    `cross_check_anchors=True` -- deliberately not `INTEGRATION_HONESTY`, where 4 of its 20
    resolving anchored citations already fail this predicate (measured at round FF, not swept by
    this task); see the field's docstring on `DocProfile`.
    """
    survey = surveys[profile.name]
    pinned = set(profile.text_mismatch_pins)
    bad = []
    for a in survey.anchored:
        if not a.resolves or a.key in pinned:
            continue
        token = _anchor_token(a.anchor)
        lo, hi = a.citation.span
        text = _cited_source_text(survey.root, a.citation.resolved, lo, hi)  # type: ignore[arg-type]
        if re.search(rf"\b{re.escape(token)}\b", text):
            continue
        bad.append(
            f"{a.citation.site}: `{a.anchor}` (`{a.citation.text}`) -> containment resolves "
            f"but the cited line(s) do not mention `{token}`; repoint it to the line that "
            f"actually states the claim, or add ('{a.anchor}', {a.citation.text!r}) to "
            f"{profile.name}'s text_mismatch_pins with a measurement of why the cited line is "
            "right despite not naming the anchor"
        )
    assert not bad, "anchored citations whose cited line does not name their anchor:\n" + "\n".join(
        bad
    )


_TEXT_PIN_CASES: tuple[tuple[DocProfile, tuple[str, str]], ...] = tuple(
    (p, pin) for p in _PROFILES for pin in p.text_mismatch_pins
)


@pytest.mark.parametrize(
    "case",
    _TEXT_PIN_CASES,
    ids=lambda case: f"{case[0].name}|{case[1][0]}|{case[1][1]}",
)
def test_a_pinned_text_mismatch_still_mismatches(
    case: tuple[DocProfile, tuple[str, str]], surveys: dict[str, Survey]
) -> None:
    """The other direction of the text-mismatch ratchet, one case per `text_mismatch_pins` entry.

    Mirrors `test_each_pinned_citation_is_still_unresolved`: a pin recorded because the cited
    line does not name its anchor must keep not naming it, or the pin has gone stale and should
    be deleted (the citation may have been repointed, in which case the primary check now covers
    it without this exemption).
    """
    profile, pin = case
    survey = surveys[profile.name]
    found = survey.anchored_by_key(pin)
    assert found is not None, (
        f"pinned text-mismatch citation `{pin[0]}` (`{pin[1]}`) is no longer in {profile.doc_rel} "
        f"in that form. If it was repointed, delete this entry from {profile.name}'s "
        "text_mismatch_pins."
    )
    assert found.resolves, (
        f"{found.citation.site}: `{pin[0]}` (`{pin[1]}`) is pinned as a text mismatch but no "
        "longer resolves under containment either -- it belongs in `pins`, not "
        "`text_mismatch_pins`; move it."
    )
    token = _anchor_token(pin[0])
    lo, hi = found.citation.span
    text = _cited_source_text(survey.root, found.citation.resolved, lo, hi)  # type: ignore[arg-type]
    assert not re.search(rf"\b{re.escape(token)}\b", text), (
        f"{found.citation.site}: `{pin[0]}` (`{pin[1]}`) now mentions `{token}` at the cited "
        f"line. Delete this entry from {profile.name}'s text_mismatch_pins."
    )


@pytest.mark.parametrize("profile", _PROFILES, ids=lambda p: p.name)
def test_a_commit_bound_citation_is_never_offered_for_resolution(
    profile: DocProfile, surveys: dict[str, Survey]
) -> None:
    """``(`:7181` at `53e5d8d`)`` is a claim about a past tree: correct forever, and
    repointing it would destroy the record. It carries no path, so ``_CITATION`` cannot
    match it and no check above can ever demand it resolve. This asserts that exemption
    rather than trusting it, and asserts the form is still present to be exempted.

    This is the category that made the seam necessary: a sibling document has none of these,
    so a profile copied naively reds here on day one and the tempting repair -- dropping the
    presence assertion -- would leave the file that HAS them with nothing asserting the
    exemption is still exercised. A profile for such a document declares `commit_bound`
    `not_applicable` instead, which fails the moment one appears.
    """
    survey = surveys[profile.name]
    _check_population(profile, "commit_bound", len(survey.commit_bound))
    ledger_text = (survey.root / profile.doc_rel).read_text(encoding="utf-8")
    for m in _COMMIT_BOUND.finditer(ledger_text):
        assert not _CITATION.search(m.group(0)), (
            f"a commit-bound citation {m.group(0)!r} was matched as a live citation"
        )


def _record_exempt_lines(source_lines: list[str]) -> set[int]:
    """1-based line numbers inside a ``Measured at <sha>:`` record paragraph.

    The paragraph runs from the marker line to the end of its contiguous block -- the first
    following line that is blank, or that is not a ``#`` comment when the marker sits in one.
    Derived from the text, not from a list of sites: a new record paragraph is exempt the
    moment it is written, and moving one does not need this function edited.
    """
    exempt: set[int] = set()
    for i, line in enumerate(source_lines):
        if not _RECORD_MARKER.search(line):
            continue
        in_comment = line.lstrip().startswith("#")
        j = i
        while j < len(source_lines):
            nxt = source_lines[j].strip()
            if j > i and (not nxt or (in_comment and not nxt.startswith("#"))):
                break
            exempt.add(j + 1)
            j += 1
    return exempt


def _categories_consulted_by_tests() -> set[str]:
    """The categories this module's test bodies actually consult, read out of its own AST.

    Derived from what the code **does** -- a string literal handed to a `_CATEGORY_ACCESSORS`
    call inside a `test_*` function -- not from where the text sits and not from a hand list.
    This project measured the cost of the other shape: a set derived from file membership
    plus name classified a real writer as machinery, and the closure test stayed green over
    a sentence it falsifies.

    Stated blind spot: a category consulted through an accessor not named in
    `_CATEGORY_ACCESSORS`, or with a computed rather than literal key, is invisible here --
    which is why the covering test compares this set against `_CATEGORIES` rather than
    trusting it alone. A missed accessor makes that comparison unequal and fails loudly.
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func: Any = inner.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name not in _CATEGORY_ACCESSORS:
                continue
            for arg in inner.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.add(arg.value)
    return found


def test_every_profile_declares_every_assertion_explicitly() -> None:
    """No profile may leave a category undeclared, and no declaration may be a bare waiver.

    The quantity watched is **agreement between the categories the checks consult and the
    categories each profile declares**. An omitted declaration cannot leave it unchanged:
    `_disposition` would find zero entries and the check over that category would raise
    instead of quietly passing -- this test is what turns that into one named failure at the
    profile rather than a surprise inside an unrelated check.

    Fails **by name**, in both directions, and never by count.
    """
    consulted = _categories_consulted_by_tests()
    bad: list[str] = []
    if consulted != set(_CATEGORIES):
        bad.append(
            f"the categories the test bodies consult {sorted(consulted)} differ from the "
            f"declared vocabulary _CATEGORIES {sorted(_CATEGORIES)}; missing from the "
            f"vocabulary: {sorted(consulted - set(_CATEGORIES))}; consulted by no check: "
            f"{sorted(set(_CATEGORIES) - consulted)}"
        )
    seen: set[str] = set()
    for profile in _PROFILES:
        if profile.name in seen:
            bad.append(
                f"two profiles are named {profile.name!r}; the survey fixture is keyed on that "
                "name, so one would silently replace the other and its document go unchecked"
            )
        seen.add(profile.name)
        declared = [k for k, _ in profile.dispositions]
        for key in sorted(set(consulted) - set(declared)):
            bad.append(
                f"{profile.name}: category {key!r} is consulted by a check in this module but "
                "this profile declares no disposition for it"
            )
        for key in sorted(set(declared) - set(consulted)):
            bad.append(
                f"{profile.name}: declares category {key!r}, which no check in this module "
                "consults -- a dead declaration asserts nothing"
            )
        for key in sorted(set(declared)):
            disposition = _disposition(profile, key)
            if disposition.kind not in (_ASSERT, _NOT_APPLICABLE):
                bad.append(
                    f"{profile.name}: category {key!r} has unknown disposition {disposition.kind!r}"
                )
            if disposition.kind == _NOT_APPLICABLE and not disposition.reason.strip():
                bad.append(
                    f"{profile.name}: category {key!r} is `{_NOT_APPLICABLE}` with no reason. "
                    "The reason is what a later author checks the claim against; without it "
                    "the declaration is a waiver wearing a mechanism's clothes"
                )
    assert not bad, "profile declarations are incomplete:\n" + "\n".join(bad)


def test_a_not_applicable_declaration_is_falsifiable_and_an_assert_one_cannot_be_vacuous() -> None:
    """The mechanism's own test, on a **synthetic** profile -- no real document is involved.

    The landed profile declares every category `assert`, so nothing above exercises the
    `not_applicable` branch; without this, that branch would be shipped unmeasured. Both
    directions are checked here, on the same synthetic profile, so the pair is the
    discriminator: a `pytest.skip` in place of this mechanism passes the empty case and
    passes the non-empty one too, and that second cell is the whole difference between an
    empty category and an unchecked one.
    """
    empty_claimed = DocProfile(
        name="SYNTHETIC",
        doc_rel="docs/does-not-exist.md",
        search_dirs=("src",),
        pins=(),
        dispositions=(("commit_bound", _not_applicable("this document has none of them")),),
    )
    _check_population(empty_claimed, "commit_bound", 0)
    with pytest.raises(AssertionError, match="this document has none of them"):
        _check_population(empty_claimed, "commit_bound", 1)

    present_claimed = DocProfile(
        name="SYNTHETIC",
        doc_rel="docs/does-not-exist.md",
        search_dirs=("src",),
        pins=(),
        dispositions=(("commit_bound", _asserted()),),
    )
    _check_population(present_claimed, "commit_bound", 1)
    with pytest.raises(AssertionError, match="vacuous quantification"):
        _check_population(present_claimed, "commit_bound", 0)

    duplicated = DocProfile(
        name="SYNTHETIC",
        doc_rel="docs/does-not-exist.md",
        search_dirs=("src",),
        pins=(),
        dispositions=(
            ("commit_bound", _asserted()),
            ("commit_bound", _not_applicable("shadowing the assert above")),
        ),
    )
    with pytest.raises(AssertionError, match="declares 2 dispositions"):
        _check_population(duplicated, "commit_bound", 0)


def test_every_census_number_this_module_states_is_the_number_it_derives(
    surveys: dict[str, Survey],
) -> None:
    """This module's prose is an input to the next author, so it is checked like code.

    The defect this exists to stop is measured, not hypothetical: the docstring above said the
    census was one number while the comment on ``_SEARCH_DIRS`` said another, both landed in the
    same commit, and **every check in this file passed the whole time** -- because the quantity
    nothing watched was *agreement between what this module says about its census and what it
    derives*. A self-contradiction cannot leave that quantity unchanged: the two statements are
    the same class, so one of them must differ from the derivation.

    The claim is parsed **out of** the prose and checked against the survey, so editing the
    sentence changes what is asserted; a text-to-text comparison would only prove the copies
    agree, and copies agree on a false sentence just as readily. Matched over the
    whitespace-normalised source with offsets mapped back to line numbers -- both census
    sentences in this file are line-wrapped, and a line-oriented sweep sees neither.

    Stated blind spots: a census claim worded outside ``_CENSUS_CLAIM``'s vocabulary is invisible
    here, and a claim inside a ``Measured at <sha>:`` record is exempt by rule and unchecked.
    A third: this module's prose states a census for **one** profile, named in
    ``_CENSUS_PROSE_PROFILE``, so a second profile's census would not be stated here and would
    not be checked. Naming it is what stops a second profile silently re-pointing this check.
    """
    profile = next((p for p in _PROFILES if p.name == _CENSUS_PROSE_PROFILE), None)
    assert profile is not None, (
        f"_CENSUS_PROSE_PROFILE names {_CENSUS_PROSE_PROFILE!r}, which is not a profile in "
        f"_PROFILES ({[p.name for p in _PROFILES]}), so this module's prose census is about "
        "nothing that is measured"
    )
    source = Path(__file__).read_text(encoding="utf-8")
    norm, omap = _normalised(source)
    exempt = _record_exempt_lines(source.split("\n"))
    derived = sum(1 for a in surveys[profile.name].anchored if not a.resolves)

    checked: list[int] = []
    wrong: list[str] = []
    for m in _CENSUS_CLAIM.finditer(norm):
        line = source.count("\n", 0, omap[m.start()]) + 1
        if line in exempt:
            continue
        checked.append(line)
        if int(m.group("n")) != derived:
            wrong.append(
                f"{Path(__file__).name}:{line}: this module states {m.group('n')} for the "
                f"anchored-unresolved census; the survey derives {derived} "
                f"({m.group(0).strip()!r})"
            )
    assert not wrong, (
        "this module contradicts its own measurement -- correct the prose, or annotate it as a "
        "`Measured at <sha>:` record if it is a claim about a past tree:\n" + "\n".join(wrong)
    )
    assert checked, (
        "no live census claim was found in this module's prose, so this check is asserting "
        "nothing. Either the census sentence was deleted, or it was reworded out of "
        f"_CENSUS_CLAIM's vocabulary ({_CENSUS_CLAIM.pattern!r})."
    )
