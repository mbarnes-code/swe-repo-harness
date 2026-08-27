"""`docs/SPEC.md` §5's WAL paragraph, bound: every claim it makes, and the code it describes.

WHY this file exists
--------------------
`4398358` corrected that paragraph. The old sentence credited `PRAGMA wal_checkpoint(TRUNCATE)`
to "the single projector task"; no code under `src/` has ever issued it. The replacement is true
and measured (D81's 2026-08-25 marker in `docs/INTEGRATION_HONESTY.md` carries the fixture), but
D81 stayed `PARTLY ADDRESSED` for one reason, in its own words: the pragma half *is* bound —
`tests/test_db.py` reads `wal_autocheckpoint` back off both a live read handle and a live write
handle — while **nothing bound the load-bearing structural claim**, *"the tree's only read
transaction is `build_state`'s `BEGIN DEFERRED`"*. A second `BEGIN DEFERRED` added anywhere under
`src/` would falsify the SPEC silently and re-open a hazard that was measured at **8.12x** WAL
growth. The paragraph's own *"no code path may hold a read transaction open across a wave"* was a
stated rule with no mechanism, and was recorded as one rather than dressed up as enforced.

This file is that mechanism. It follows `tests/test_floor_rule_statements.py`, which is the
landed shape: **parse the claim out of the prose and check the parsed values against the code**,
so that editing the prose changes what is asserted and a false edit fails. Nothing here compares
one copy of a sentence to another copy; text-to-text agreement is satisfied by two copies of a
falsehood.

The quantity this instrument watches
------------------------------------
**The set of transaction-control statements that reach SQLite from `src/`, keyed
`(relpath, line, enclosing function, mode)` and derived from what the code *does*, not from where
its text sits.** That is the choice round G's measurements force: *file exists* and *line in
range* are invariant under drift, and *token in range* certified a method green by coincidence.
The defect being hunted is "a second read transaction exists". A read transaction cannot exist
without a `BEGIN` (bare, `DEFERRED`, or `TRANSACTION`) reaching a connection, so it cannot leave
this set unchanged — that is the whole reason this quantity was chosen over a count of matching
lines, which a wrapped or renamed site moves without the hazard moving.

Two genuinely different derivations, cross-checked in both directions
---------------------------------------------------------------------
1. **`ast`, from the body.** Every `Call` whose callee name is in the execute family and whose
   first argument resolves to a string beginning with a `BEGIN` keyword. Module-level `X = "..."`
   constants are resolved, because CLAUDE.md records the exact refactor that defeats a
   literals-only reader: moving one statement into a module constant took a derived set from
   3 sinks to 1 with a `+7/-2` diff and every test green.
2. **Every SQL-script literal in the tree.** Any `BEGIN` statement inside one must appear in
   derivation 1. This is the recognition-gap arm: a transaction statement arm 1 could not tie to an
   execute call fails **by `file:line`** rather than vanishing. It is what catches
   `SQL = "BEGIN DEFERRED"` defined in one module and executed in another, and it is why a broken
   arm 1 cannot report a clean tree. *"SQL-script literal"* is load-bearing and is defined
   structurally in `_sql_script_literals`: not a docstring, and every `;`-separated fragment opens
   with a SQL keyword. Arm 2 read *whole literals* until `_statements` split scripts into
   fragments, at which point it began firing on documentation; the gate is what makes it safe to
   read fragments.

Prose that mentions `BEGIN DEFERRED` inside a docstring is not a transaction. Arm 2 separates the
three occurrences of `BEGIN DEFERRED` in `state/projection.py` into one executed site and two
pieces of documentation **by excluding docstrings structurally and requiring every fragment of a
literal to open with a SQL keyword** -- not, as this paragraph said until a review measured it
false, by the literal being "nothing but the statement". That older rule died when `_statements`
began splitting on `;`, and the sentence describing it outlived it by one round.

The census anchor is older than the correction
----------------------------------------------
The block is located by `**Retention and on-disk ceiling.**` and terminated by
`Expected steady-state ceiling for a 250-repo run: **< 2 GB**`. Both bracket phrases are present
in the pre-`4398358` text (verified against `git show 4398358 -- docs/SPEC.md`), so a revert of
the paragraph to the wrong sentence is still **found** by the census and then fails by name at the
claim parse. Anchoring on the corrected words instead would make a revert read as "no such
paragraph", i.e. as a deletion, and pass.
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = _ROOT / "docs" / "SPEC.md"
_SRC = _ROOT / "src"
_PKG = _SRC / "fleet"

_RESIDUAL = """Not bound, stated rather than implied:

1. **Scope is `src/`, because that is the scope the sentence claims.** A read transaction opened
   by a test, by a script, or by an operator's `sqlite3` shell overlapping a wave is outside every
   assertion here. D81's marker already records that last one as unmeasured and inherited as
   unmeasured; this file does not change that.

2. **A `BEGIN` assembled at runtime is invisible to every arm.** `conn.execute(f"BEGIN {mode}")`,
   a statement built by `"".join(...)`, or an `executescript` of a `.sql` file read from disk would
   open a read transaction that no derivation here reports. All of them are static. Nothing in the
   tree assembles a transaction statement at runtime today, and that is a mitigation, not a
   mechanism -- which is exactly the sentence the escape below proved this item had been leaning
   on too hard.

   **This item previously claimed more scope than it had, and a review proved it.** A multi-
   statement `executescript` literal -- no f-string, no file read, and `executescript` already in
   `_EXECUTE_CALLS`, so the instrument presented itself as handling exactly that call -- escaped
   all nine cases green. That is not this item's shape and it was not adversarial: it is ordinary
   code a normal author writes, so CLAUDE.md's stop rule says fix, not document. Fixed by
   `_statements`; M14 is the escape re-run as a synthetic fault and it now reddens the uniqueness
   case alone. The residual that genuinely remains is the runtime-assembly sentence above.

3. **An implicit read transaction opened without `BEGIN` is not seen.** This file asserts on
   explicit transaction control. A driver configured to open transactions implicitly would hold
   one with no `BEGIN` anywhere. `state/db.py` and `state/projection.py` both drive transactions
   explicitly today, which is the premise this rests on, not a thing it checks.

4. **The paragraph's caller enumeration is deliberately NOT asserted, and it is incomplete.**
   The sentence says the read transaction runs "on a handle `project_once` opens and closes around
   one snapshot and that the <= 1 Hz `MIN_PROJECTION_INTERVAL_S` debounce re-opens at most once a
   second". Measured at `94a2653`: `build_state` has **two** callers under `src/` —
   `state/projection.py:350` inside `project_once`, and one more in `src/fleet/cli.py`, a
   one-shot read-only command that is not debounced at all. Binding the enumeration as written
   would make this file red over a sentence whose *hazard* claim is nonetheless true, so what is
   bound instead is the property that makes the enumeration non-load-bearing:
   `test_the_read_transaction_is_terminated_in_the_function_that_opens_it`. No caller can hold the
   transaction past `build_state`'s return, so which callers exist does not change the bound. The
   incompleteness is reported rather than edited: correcting SPEC prose was outside this lane.

5. **A consistent rewrite of the whole paragraph to one false sentence.** The parsed claims are
   the five below; a falsehood outside them is caught by nothing here. This is the same residual
   `tests/test_floor_rule_statements.py` records, for the same reason: parsing is what reaches the
   code, and it only reaches as far as what it parses.

6. **Severity figures are not bound.** "~4 MB per 1 000 write transactions", the flat "~4 MB
   plateau" and the "< 2 GB" ceiling are the outputs of one fixture recorded in D81. Binding them
   would mean re-running a WAL-growth fixture in the suite; the numbers are labelled an order of
   magnitude in the ledger and are treated as prose here.

7. **The mode-vocabulary control's 40-character cap is gone, and this records what it cost.**
   That control exists because both derivations share `_BEGIN`'s alternation, and a shared
   alternation is a shared blind spot: dropping `IMMEDIATE` from it left the whole file green
   (M10). The cap was standing in for "statement or prose?", and a review measured it as **the
   loud/silent divide of a live escape** -- a 26-character transaction statement went RED, a
   47-character one gave **9 passed**. It is deleted, and `_sql_script_literals` answers the
   question structurally instead. What remains: the control sees only literals that gate says are
   SQL scripts, so a transaction statement in a literal that gate rejects is invisible to it as
   well as to arm 2 -- the two now share *that* assumption, which is item 11's subject.

8. **Everything here is static.** No WAL file is grown and no transaction is executed. D81's
   hazard was established with a runtime fixture (8 000 write transactions per arm, a held reader
   byte-identical to the autocheckpoint-disabled arm at all eight samples); this file binds the
   SPEC's *structural* claim about how many read transactions the source contains. It is not a
   reproduction of the physics and must not be cited as one.

9. **A syntax error anywhere under `src/` makes five of the nine cases raise from `ast.parse`.**
   That is a per-case error and not a module-import outage, deliberately: CLAUDE.md records a
   module that took 20 passing checks to 0 executed because its locator ran at import. Confirmed
   by review at 5 failed / 4 passed with the module imported. The cost is five noisy failures
   beside the real one. Separately, and **pre-existing rather than introduced here**: a syntax
   error in a module `tests/conftest.py` imports kills the whole session at exit 4, which no
   arrangement inside this file can soften.

10. **This enumeration is a floor, not a ceiling, and has been measured as one.** Two of the
   escapes above -- the multi-statement `executescript` and a `commit`/`rollback` pair moved onto
   a different object while the name of the test asserting termination stayed true-sounding --
   were escapes from *load-bearing assertions this list already claimed to cover*, and neither was
   found by the author. Both are fixed and both now have a mutation that reddens exactly one case
   (M14, M15). The honest reading is that a reviewer with fresh eyes found two in one pass, so the
   probability that this list is complete is low; treat it as what has been measured, never as a
   boundary of what can escape.

11. **NOTHING ABOVE DESCRIBES WHAT THIS INSTRUMENT WRONGLY CATCHES, AND THAT WAS ITSELF A GAP.**
   Every item 1-10 describes something the instrument *misses*. A review pointed out that the
   false-positive direction had no entry at all -- while a live false positive existed. It does
   now, here and in item 12. The general statement: this file can redden a **correct** tree, and
   when it does the failure is worse than a miss, because a detector that fires on correct code
   teaches the next author to weaken it. Two concrete routes are known.

   The first is closed and is why this item exists. Widening arm 2 from *whole literal* to *any
   `;`-separated fragment* made an ordinary docstring listing a retention script -- a `BEGIN`
   following a `;`-terminated line -- redden a clean tree (gate +12/-0, measured). Closed by
   `_sql_script_literals`; M16 is that docstring kept as a permanent control and it must stay
   **green**.

12. **A `;` inside a quoted SQL string is a live false positive, disclosed and NOT closed.**
   `_statements` splits on `;` without parsing string quoting, so a statement such as
   `INSERT INTO t VALUES ('a;BEGIN DEFERRED;b')` splits into fragments one of which reads exactly
   `BEGIN DEFERRED`, and the census reports a second reader that does not exist. This was written
   in `_statements`' own docstring from the moment the split landed and was **left out of this
   list**, which is exactly the gap item 11 names -- a disclosure filed where the reader of the
   residual will not see it is half a disclosure.

   **The trade is stated rather than closed, and the reason below is the second one written here
   -- the first was false.** This item used to say closing it "requires a quote-aware SQL lexer",
   i.e. that it could not be done cheaply. **That was never measured and it is wrong:** a reviewer
   built an 18-line quote-aware scanner that closes M17 while keeping the `executescript` escape
   RED. The thing this item called impossible is possible, and the sentence asserting otherwise
   was a supporting rationale that was never the claim under test -- which is exactly how it went
   unmeasured.

   **The measured reason the trade survives is different and better: that scanner buys the loud
   wrong verdict back with a SILENT one.** Under it an unbalanced quote goes from shipped-RED to
   prototype-green -- a second reader that the shipped instrument catches and the "fixed" one does
   not. Trading a loud wrong verdict for a silent miss is the trade this file already refused once
   (`_sql_script_literals`' own docstring records the refusal), and it is refused again here for
   the same reason: a false RED is argued with and fixed, a false GREEN is believed.

   **The boundary is legitimate because the shape is unreachable, and that is measured, not
   assumed.** Of the executed SQL literals under `src/`, **zero contain a `;` at all** -- this
   lane counts **87** such literals at `238a0d9`, a reviewer independently counts **75**; the raw
   totals differ with the predicate and the class result ("none of them contains a semicolon") is
   identical and is the part that carries the claim. So the instrument is deliberately biased
   toward **firing loudly on a shape nothing in the tree writes** rather than staying silent on
   one a review demonstrated. M17 is that shape as a mutation: it reddens the uniqueness case, and
   its verdict is **wrong on purpose**, recorded here so a future reader meeting it knows it is a
   known cost and not a discovery.

13. **The `_sql_script_literals` gate bought a bounded SILENCE in arm 2, and this is where it is
   disclosed rather than in a docstring.** A quoted `;` inside an ordinary script makes at least
   one `;`-fragment fail the SQL-keyword test, so the gate rejects the whole literal and arm 2
   never looks at the `BEGIN` in it. Measured on one input at `238a0d9`, gate +3/-0 -- a dead
   literal `"BEGIN DEFERRED; INSERT INTO runs (run_id) VALUES ('a;b'); COMMIT;"`:

   * **pre-fix arm 2** (the same file with the gate's two conditions disabled): **RED** at
     `test_every_transaction_literal_is_tied_to_an_execute_call`;
   * **shipped**: **9 passed** -- silent.

   Old-fails / new-passes on one input, which is this project's own discriminating shape pointed
   the other way, and it is a real cost of closing the docstring false positive.

   **The bound, measured rather than asserted:** the silence covers only the *dead or untied* form.
   The **same** literal handed to an execute call (gate +4/-0) is caught by **arm 1**, which does
   not consult the gate: the uniqueness case goes RED. So what escapes is a transaction statement
   sitting in a script that carries a quoted `;` **and** that nothing executes through a call this
   file can resolve -- both conditions at once. Arm 2 is the recognition-gap net, so what is lost
   is the net under the net, not the primary census.

14. **Comment stripping is NOT quote-aware, and that buys a bounded SILENCE -- the bad direction.**
   `_strip_sql_comments` closed a live escape that was pre-existing at `924b159`: a second executed
   reader whose `BEGIN` followed a leading `--` comment left this file **10 passed**, and so did
   `/* */` inline, `/* */` spanning lines, and the `--`-then-`executescript`-script form. All three
   recognition arms shared one assumption -- *a statement's text opens with its own first keyword*
   -- and it is removed once, in `_statements`, rather than patched in the arm that was exercised.

   What it costs: a `--` inside a **quoted** SQL string blanks the rest of that line, so a real
   `BEGIN` after it on that line is hidden. Unlike item 12's `;`, whose mis-split fires **loudly**,
   this one is **silent**, and silence is the direction this file has twice refused to trade
   toward.

   **Measured as a flip, not described.** This was disclosed in words for a round before anyone
   ran it, which is the sibling of the failure item 12 names: a disclosure the reader *can* see but
   that was never measured. The input is live SQL (verified against sqlite 3.45.1, `executescript`
   accepts it)::

       "SELECT tag FROM runs WHERE tag = '--'; BEGIN DEFERRED; SELECT 1 FROM runs; COMMIT;"

   Executed under `src/` it is **RED pre-fix** (uniqueness case) and **GREEN post-fix**, gate
   +5/-0. That is old-fails / new-passes on one input -- this project's discriminating shape
   pointed the wrong way -- and it is the honest cost of this item, stated as a number rather than
   as a caveat.

   It is admitted for one reason, and it is a reachability bound rather than a construction
   one: of the **87** strings reaching an execute-family call under `src/` at `924b159`, **zero
   contain `--`, zero contain `/*` and zero contain `;`** -- the same class result item 12 rests
   on, re-derived here by an independent AST walk. The raw total is predicate-dependent (a reviewer
   counted 75 for item 12's version of it); the class result is the part that carries the claim.

   The alternative was rejected on measurement, not taste: a quote-aware strip would let an
   **unbalanced** quote swallow the rest of a script silently, converting a shipped loud verdict
   into a miss -- item 12's refusal arriving by a different token. The block form is already biased
   loud: `/\\*[\\s\\S]*?\\*/` requires its terminator, so an unterminated `/*` strips nothing.

   **The comment class is closed rather than merely reduced, and that is checkable.** SQLite has
   exactly two comment forms, and both are handled in every position: leading, trailing, and
   *interior* -- `BEGIN /* mode */ DEFERRED` is real SQLite that opens a read transaction, and it
   is why comments are blanked to a **space** rather than to nothing. Two shapes that look like
   members and are not: `#` is not a SQLite comment (`unrecognized token: "#"`) and block comments
   do not nest (`/* a /* b */ */` is a syntax error). All five verified against sqlite 3.45.1.

   **What this item does NOT claim is that the widening is free in the other direction.** Stripping
   comments lets `_sql_script_literals` see a literal's real first keyword, and widening that gate
   is what produced this file's one historical loud-wrong failure (item 11). Measured at `924b159`
   by loading the pre-fix and post-fix modules side by side against the same tree: the gate's
   admitted set is **identical, 315 literals before and 315 after, with 0 admitted only after**,
   and arm 2's `BEGIN` sites are the same three.

   **The cost this widening can carry is NOT "a literal is newly admitted" -- it is a RED ON
   DOCUMENTATION, and that wording matters because it names the failure this file has already
   committed once.** A review supplied the case, and it is a flip, gate +3/-0::

       _CR2_USAGE = "-- fleet resume --\nBEGIN by choosing a wave"

   Pure prose, not a docstring, nothing to do with SQL. **GREEN pre-fix, RED post-fix** -- and it
   fails at the mode-vocabulary control claiming *"a transaction mode has dropped out of the
   recognition vocabulary"*, which is a false verdict about the instrument on a literal that is not
   a statement at all. That is item 11's route, re-opened by one token.

   It is acceptable **only because the class is empty and stays checked**: of the **8781**
   non-docstring string literals under `src/` at `924b159`, **295 open with `--` or `/*` and 0 are
   newly admitted, 0 newly RED** (independent AST walk; a reviewer counting only `--` reports 357
   carrying it against this walk's 388 for either token -- predicate-dependent raw totals, one
   class result). Emptiness of a class today is not a property, which is why **M16 stays a
   permanent control** rather than being retired as settled.

"""


def _flex(literal: str) -> str:
    """`literal` as a regex whose inter-word gaps match any run of whitespace.

    The paragraph wraps at 98 columns and three of the five claims below already straddle a line
    break in the shipped text. Matching against the raw file with literal spaces would make a
    cosmetic re-wrap fail, which would make this instrument assert layout rather than meaning.
    """
    return r"\s+".join(re.escape(word) for word in literal.split())


def _normalise(text: str) -> str:
    return " ".join(text.split())


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


# Both bracket phrases pre-date `4398358` and survived it unchanged; see the module docstring.
_BLOCK_OPENS = _flex("**Retention and on-disk ceiling.**")
_BLOCK_CLOSES = _flex("Expected steady-state ceiling for a 250-repo run: **< 2 GB**")
#: No length bound between the two anchors, deliberately. The first version carried `{0,4000}`,
#: which is exactly the Rule 11 hazard: a magic number that does not derive, names neither the
#: measured length nor the limit, and -- worst -- fails by *misdiagnosing* the paragraph as
#: "0 site(s)" rather than saying the bound was hit. Measured before deleting it: the match is
#: 1199 characters and the region between the anchors is 1105, so the bound had 2895 characters of
#: headroom and would have rotted silently on growth. Rule 11's durable fix deletes the number
#: rather than raising it. Straddling is prevented structurally instead, by the paragraph-break
#: assertion in `_spec_block` -- a claim, not an arithmetic guess.
_BLOCK = re.compile(_BLOCK_OPENS + r"[\s\S]*?" + _BLOCK_CLOSES)

#: The uniqueness claim: quantifier, cited symbol, transaction mode, cited module. The word
#: "only" sits inside the anchored phrase, so a paragraph that stops claiming uniqueness fails
#: here loudly instead of yielding a weaker claim that still compares equal.
_ONLY_READ = re.compile(
    _flex("The tree's only read transaction is")
    + r"\s*`(?P<symbol>\w+)`'s\s*`(?P<mode>BEGIN(?:\s+\w+)?)`\s*\(`(?P<module>[\w/.]+)`\)"
)

#: The WAL bound: the pragma value, and the module and constant said to carry it.
_AUTOCHECKPOINT = re.compile(
    _flex("The WAL's only bound is")
    + r"\s*`PRAGMA\s+wal_autocheckpoint\s*=\s*(?P<value>\d+)`"
    + r"\s*\(`(?P<module>[\w/.]+)`'s\s*`(?P<const>\w+)`\)"
)

#: The denial the correction put in place of the false attribution, with its own scope parsed out.
_DENIAL = re.compile(
    _flex("nothing under") + r"\s*`(?P<scope>[\w/]+)`\s*issues\s*`PRAGMA\s+(?P<pragma>\w+)`"
)

#: The debounce bound, as a rate and a named constant.
_DEBOUNCE = re.compile(
    r"[≤<]=?\s*(?P<hz>[\d.]+)\s*Hz\s*`(?P<const>\w+)`\s*debounce"
)

_EXECUTE_CALLS = frozenset(
    {
        "execute",
        "executescript",
        "executemany",
        "execute_insert",
        "execute_fetchall",
        "executescript_fetchall",
    }
)

#: `BEGIN` without a mode is `DEFERRED` in SQLite, and so is `BEGIN TRANSACTION`; both are
#: readers until they write. `IMMEDIATE`/`EXCLUSIVE` take a write lock at statement start.
_BEGIN = re.compile(r"^BEGIN(?:\s+(?P<mode>DEFERRED|IMMEDIATE|EXCLUSIVE|TRANSACTION))?\s*;?$", re.I)
_READ_MODES = frozenset({"", "DEFERRED", "TRANSACTION"})


#: SQLite has exactly two comment forms: `--` to end of line, and `/* ... */`, which may span
#: lines. Both are matched here; the block form is non-greedy and **requires its terminator**, so
#: an unterminated `/*` strips nothing rather than swallowing the rest of the script -- the loud
#: direction, chosen for the reason `_RESIDUAL` item 12 gives.
_SQL_COMMENT = re.compile(r"--[^\n]*|/\*[\s\S]*?\*/")


def _strip_sql_comments(sql: str) -> str:
    """`sql` with its SQL comments replaced by a space.

    **This is the root of an escape a review measured, and it is removed once here rather than
    patched in the three arms that suffered it.** Every recognition step in this file decides what
    a statement is by reading its *first characters* -- `_BEGIN` anchors `^BEGIN`,
    `_sql_script_literals` takes `statement.split(None, 1)[0]`, and `_statements_opening_with_begin`
    anchors `^BEGIN\\b`. All three therefore shared one assumption: **a statement's text opens with
    its own first keyword.** A SQL comment falsifies that, and SQLite does not care --
    `conn.execute("-- refresh the snapshot\\nBEGIN DEFERRED")` opens a read transaction (measured on
    sqlite 3.45.1: `in_transaction` True through both `execute` and `executescript`, for `--`,
    for `/* */` inline, and for `/* */` spanning lines).

    Measured at `924b159` before this existed: a second live reader whose `BEGIN` follows a leading
    `--` comment left the file **10 passed**, and so did all three of the other comment shapes. The
    one shape that was *not* silent was a **trailing** comment, and it was worse than silent in a
    different way -- it reddened the mode-vocabulary control with the message "a transaction mode
    has dropped out of the recognition vocabulary", which is a misdiagnosis of a live second
    reader. Stripping first makes that site fail as what it is.

    Blanked to a space, not to nothing, so that `BEGIN/*x*/DEFERRED` reads `BEGIN DEFERRED` and not
    `BEGINDEFERRED`.

    The cost, disclosed here and in `_RESIDUAL` item 14: this is not quote-aware, for the same
    reason the `;` split below is not (item 12 -- a quote-aware scanner trades a loud wrong verdict
    for a silent miss on an unbalanced quote). So a `--` inside a quoted SQL string blanks the rest
    of its line. That direction is **silent**, which is the bad one; it is bounded by reachability
    rather than by construction, and the bound is measured, not assumed: of the **87** strings that
    reach an execute-family call under `src/` at `924b159`, **zero contain `--`, zero contain `/*`
    and zero contain `;`** -- the same class result item 12 rests on, re-derived here.
    """
    return _SQL_COMMENT.sub(" ", sql)


def _statements(sql: str) -> list[str]:
    """`sql` with its comments stripped, split into its `;`-separated statements, stripped,
    empties dropped.

    **Every recognition step in this file goes through here, and the first version did not have
    it.** `_BEGIN` anchors `^...$`, so it reads a whole literal as one statement -- and a review
    escaped the entire module with ordinary code::

        await conn.executescript("BEGIN DEFERRED;\\nSELECT 1 FROM runs;\\nCOMMIT;")

    appended to `state/checkpoints.py` opened a second read transaction with all nine cases green.
    Re-derived here rather than inherited: `_BEGIN` returns `None` on the whole literal because of
    the trailing statements, arm 2's whole-literal rule fails for the same reason, and the mode-
    vocabulary control missed it too -- the script is 43 characters and that control only considers
    literals short enough to be a single statement. Three recognition steps, one shared assumption
    ("a literal is one statement"), which is the shared-blind-spot shape Guardrail 6 warns about
    and the second time this file has been caught by it.

    `executescript` executes exactly a `;`-separated script, so splitting on `;` is what the call's
    own semantics mean; `execute` rejects multiple statements, so splitting one is a no-op there.
    The cost, disclosed rather than hidden: a `;` inside a quoted SQL string mis-splits. In the
    direction that matters that is harmless -- a mis-split fragment simply fails `_BEGIN` -- but a
    fragment reading exactly `BEGIN DEFERRED` inside a quoted string would be a false positive.
    This file would rather fire loudly than miss another escape.

    **A third recognition step ran through here and was still escaped**, by the same shape one
    level down: comments. `_strip_sql_comments` above is that fix, and it is applied before the
    split so that every arm inherits it -- the split itself must see a `;` that a `--` comment
    would otherwise have hidden behind it.
    """
    return [part.strip() for part in _strip_sql_comments(sql).split(";") if part.strip()]



#: Every `(relpath, line)` arm 1 tied to an execute call — the call site, and the definition line
#: of any module constant it resolved through. Populated by `_transactions`, read by arm 2.
_TIED: set[tuple[str, int]] = set()


def _py_files() -> list[Path]:
    return sorted(_SRC.rglob("*.py"))


def _enclosing(tree: ast.Module) -> dict[int, str]:
    """`id(node) -> enclosing function name` for every node under a function definition."""
    owner: dict[int, str] = {}

    def walk(node: ast.AST, name: str) -> None:
        for child in ast.iter_child_nodes(node):
            here = (
                child.name
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                else name
            )
            owner[id(child)] = here
            walk(child, here)

    walk(tree, "<module>")
    return owner


def _module_constants(tree: ast.Module) -> dict[str, tuple[str, int]]:
    """Module-level `NAME = "literal"` bindings as `name -> (value, definition line)`, so a
    statement hoisted into a constant is still resolved. CLAUDE.md records this exact refactor
    silently emptying a derived sink set with a `+7/-2` diff and every test green.

    The definition line is carried because arm 2 needs it: under that refactor the literal and the
    execute call sit on different lines, and an arm-2 check keyed only on the call site reports the
    constant as an untied orphan. That is not hypothetical — it is what the refactor control
    measured against the first version of this file.
    """
    consts: dict[str, tuple[str, int]] = {}
    for stmt in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(stmt, ast.Assign):
            targets, value = list(stmt.targets), stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            for target in targets:
                if isinstance(target, ast.Name):
                    consts[target.id] = (value.value, value.lineno)
    return consts


def _callee_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _transactions() -> tuple[list[tuple[str, int, str, str]], list[tuple[str, int, str, str]]]:
    """Arm 1: `(relpath, line, enclosing function, mode)` for every executed `BEGIN` under `src/`.

    Returns `(readers, writers)`. `writers` is the live positive control: an extractor that has
    silently stopped resolving execute calls reports both lists empty, and
    `test_the_transaction_extractor_is_not_silently_broken` reads that before any verdict.
    """
    readers: list[tuple[str, int, str, str]] = []
    writers: list[tuple[str, int, str, str]] = []
    _TIED.clear()
    for path in _py_files():
        rel = path.relative_to(_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner = _enclosing(tree)
        consts = _module_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if _callee_name(node.func) not in _EXECUTE_CALLS:
                continue
            first = node.args[0]
            sql: str | None = None
            defined_at: int | None = None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                sql = first.value
            elif isinstance(first, ast.Name) and first.id in consts:
                sql, defined_at = consts[first.id]
            if sql is None:
                continue
            matched = False
            for statement in _statements(sql):
                hit = _BEGIN.match(statement)
                if hit is None:
                    continue
                matched = True
                mode = (hit.group("mode") or "").upper()
                entry = (rel, node.lineno, owner.get(id(node), "<module>"), mode)
                (readers if mode in _READ_MODES else writers).append(entry)
            if matched:
                _TIED.add((rel, node.lineno))
                if defined_at is not None:
                    _TIED.add((rel, defined_at))
    return sorted(readers), sorted(writers)


#: The keywords a SQLite statement can open with. Used to decide whether a literal is a SQL script
#: at all -- see `_sql_script_literals`. Deliberately a closed list of statement openers rather
#: than "looks SQL-ish": a whitelist that must be extended to admit a new shape is the honest way
#: round, since the failure of an over-narrow list is a missed site the recognition-gap arm reports,
#: while the failure of an over-wide one is the false positive this list exists to stop.
_SQL_OPENERS = frozenset(
    {
        "BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE",
        "SELECT", "INSERT", "REPLACE", "UPDATE", "DELETE", "WITH",
        "CREATE", "DROP", "ALTER", "REINDEX", "VACUUM", "ANALYZE",
        "PRAGMA", "ATTACH", "DETACH", "EXPLAIN",
    }
)


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """`id()` of every literal that is a docstring -- the first statement of a module, class or
    function. Structural, not textual: a docstring is a position in the tree, not a shape."""
    docs: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docs.add(id(first.value))
    return docs


def _sql_script_literals() -> list[tuple[str, int, list[str]]]:
    """Every string literal under `src/` that is a SQL script, as `(relpath, line, statements)`.

    **This gate is the fix for a false positive a review measured, and the reason it exists is
    worth more than the gate.** Arm 2 used to accept any literal whose *entire* stripped value was
    a `BEGIN` statement. When `_statements` was added to close the multi-statement `executescript`
    escape, it widened arm 2 from *whole literal* to *any fragment* -- and an ordinary docstring
    listing a retention script, whose `BEGIN` follows a `;`-terminated line, then reddened a
    **correct tree** (gate +12/-0, measured). That is strictly worse than the escape it replaced:
    the escape was silent, this was loud and wrong, and a detector that fires on correct code
    trains the next author to weaken it.

    A literal is a SQL script only if **both** hold, and they are independent:

    1. it is **not a docstring** -- a position in the tree, so a prose block cannot argue its way
       past it; and
    2. **every** one of its `;`-separated fragments opens with a SQL keyword -- so a listing
       embedded in prose is excluded by the prose around it, not by a guess about its length.

    Either alone leaves a real hole: docstring-exclusion alone still admits a prose literal that is
    not a docstring (an error message, a help string), and the all-fragments rule alone still
    admits a docstring that happens to contain nothing but SQL. Both together is why the 40-
    character cap that used to stand in for this could be deleted.
    """
    found: list[tuple[str, int, list[str]]] = []
    for path in _py_files():
        rel = path.relative_to(_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docs:
                continue
            statements = _statements(node.value)
            if not statements:
                continue
            if all(
                statement.split(None, 1)[0].upper().strip("(") in _SQL_OPENERS
                for statement in statements
            ):
                found.append((rel, node.lineno, statements))
    return sorted(found)


def _begin_statements_in_sql_literals() -> list[tuple[str, int, str]]:
    """Arm 2: every `BEGIN` statement appearing in a SQL-script literal under `src/`, executed or
    not. Named for what it measures. **Retired name: `_whole_literal_begins`** -- it described a
    rule this function stopped following when `_statements` split scripts into fragments, and a
    name that describes a retired rule is what the next reconciler restores the defect from.

    That sentence is not decoration: `test_the_retired_arm_two_name_stays_retired` parses the name
    out of it and checks this module neither defines nor calls it. It was decoration until a
    review pointed out that deleting it left nine cases passing -- a convention wearing a
    mechanism's clothes, which is the thing this file exists to refuse."""
    found: list[tuple[str, int, str]] = []
    for rel, line, statements in _sql_script_literals():
        for statement in statements:
            hit = _BEGIN.match(statement)
            if hit is not None:
                found.append((rel, line, (hit.group("mode") or "").upper()))
    return sorted(found)


def _statements_opening_with_begin() -> list[tuple[str, int, str]]:
    """Every statement in a SQL-script literal under `src/` that opens with the word BEGIN.
    Deliberately independent of `_BEGIN`'s mode alternation: it is the third, genuinely different
    recognition step the positive control needs.

    **The 40-character cap this used to carry is deleted, and that closes a live escape rather
    than merely tidying a number.** The cap was standing in for "is this a statement or prose?",
    which `_sql_script_literals` now answers structurally -- and it answered badly: a review
    measured the divide at 26 characters RED, 47 characters **9 passed**, so a transaction
    statement written long enough simply walked past the control. Same Rule 11 shape as `_BLOCK`'s
    `{0,4000}`: the durable fix deletes the number rather than tuning it."""
    opener = re.compile(r"^BEGIN\b", re.I)
    return [
        (rel, line, statement)
        for rel, line, statements in _sql_script_literals()
        for statement in statements
        if opener.match(statement)
    ]


def _literal_of(node: ast.expr, module: ast.Module) -> str:
    """The string a call argument resolves to within `module`, or `""`. Literals and module
    constants only -- the same two shapes `_transactions` resolves, kept deliberately identical so
    this test cannot disagree with the census about which call opened the transaction."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        found = _module_constants(module).get(node.id)
        if found is not None:
            return found[0]
    return ""


def _spec_block() -> tuple[str, int, str]:
    """`(normalised block, line the block opens on, raw block)` — the census, by `file:line`."""
    text = _SPEC.read_text(encoding="utf-8")
    hits = list(_BLOCK.finditer(text))
    assert len(hits) == 1, (
        f"docs/SPEC.md states the retention-and-on-disk-ceiling paragraph at {len(hits)} site(s), "
        f"expected 1 (lines: {[_line_of(text, h.start()) for h in hits]}). The census anchors on "
        f"'**Retention and on-disk ceiling.**' through the '< 2 GB' ceiling, both of which "
        f"pre-date `4398358`; a paragraph reverted to the pre-correction wording is still found "
        f"here and fails at the claim parse below, by name."
    )
    hit = hits[0]
    assert "\n\n" not in hit.group(0), (
        f"docs/SPEC.md:{_line_of(text, hit.start())} -- the census match spans a paragraph break, "
        f"so it has run past its own paragraph and the claims parsed below may come from a "
        f"different one. This is the structural guard that replaced a `{{0,4000}}` length bound; "
        f"if the retention paragraph genuinely grew a blank line, split the census instead of "
        f"relaxing this."
    )
    return _normalise(hit.group(0)), _line_of(text, hit.start()), hit.group(0)


def _claim(pattern: re.Pattern[str], what: str) -> re.Match[str]:
    block, line, _ = _spec_block()
    match = pattern.search(block)
    assert match is not None, (
        f"docs/SPEC.md:{line} — the retention paragraph no longer states {what}, so nothing here "
        f"can check it against the code. If the claim was deliberately retired, retire the "
        f"assertion that reads it in the same change; do not leave it parsing an absent sentence "
        f"and passing vacuously. Paragraph as read: {block[:400]!r}"
    )
    return match


def _resolve(module_path: str) -> object:
    """`state/projection.py` -> the imported `fleet.state.projection`.

    Imported, not read as text: CLAUDE.md's stand-in rule is that a declaration read is not a
    value exercised, and the constants below are checked as the running interpreter resolves them.
    """
    assert (_PKG / module_path).is_file(), (
        f"the paragraph cites `{module_path}`, which does not exist under src/fleet/"
    )
    dotted = "fleet." + module_path.removesuffix(".py").replace("/", ".")
    return importlib.import_module(dotted)


# --------------------------------------------------------------------------------------
# The instrument's own positive control, read before any verdict below
# --------------------------------------------------------------------------------------


def test_the_transaction_extractor_is_not_silently_broken() -> None:
    """Both derivations must find the transaction statements that are known to exist.

    Why this is first: every other test in this file is a *pure-green* conclusion of the form
    "no second read transaction exists", and CLAUDE.md is explicit that a detector never observed
    firing is not evidence of absence. If `_transactions` stopped resolving execute calls — a
    renamed method, a driver change, a walk that stopped descending — it would report zero
    readers and zero writers, and every assertion below would pass over a tree it never read.
    """
    readers, writers = _transactions()
    literals = _begin_statements_in_sql_literals()
    assert writers, (
        "the ast arm found no executed write transaction anywhere under src/, which cannot be "
        "true of a tree whose single-writer path is BEGIN IMMEDIATE. The extractor is broken, "
        "not the tree; every 'no second read transaction' result in this file is void until it "
        "is fixed."
    )
    assert readers, (
        "the ast arm found no executed read transaction, so the claim it exists to bind cannot "
        "be checked. Same verdict as above: fix the extractor before reading any result here."
    )
    assert literals, (
        "the literal arm found no BEGIN statement at all under src/, so its recognition-gap "
        "cross-check below is vacuous."
    )

    # Mode vocabulary, checked WITHOUT `_BEGIN`'s alternation, because both arms above share it.
    # Measured: dropping `IMMEDIATE` from that alternation leaves both arms green — writers stay
    # non-empty on the surviving `EXCLUSIVE` site, no literal becomes an orphan (arm 2 stops
    # recognising it too), and the read census is untouched. Non-emptiness cannot see a *partial*
    # recognition loss; this can, because it asks which literals look like statements and are then
    # classified by nothing.
    unclassified = [
        f"{rel}:{line} {value!r}"
        for rel, line, value in _statements_opening_with_begin()
        if _BEGIN.match(value) is None
    ]
    assert not unclassified, (
        f"these statements in SQL-script literals open with the word BEGIN, yet `_BEGIN` "
        f"classifies none of them: {unclassified}. A transaction mode "
        f"has dropped out of the recognition vocabulary, so both derivations above are blind to "
        f"an entire class of transaction and every verdict in this file is void until it is fixed."
    )


def test_every_transaction_literal_is_tied_to_an_execute_call() -> None:
    """Arm 2 against arm 1: a `BEGIN` statement in a SQL-script literal that the ast arm could not
    tie to an execute call is a recognition gap and is named by `file:line`, not dropped.

    A transaction statement sitting in a SQL script exists to be executed. If arm 1 cannot see the
    call — the statement was hoisted into a constant this walk does not resolve, passed through a
    helper, or handed to a driver method outside the execute family — the site fails here rather
    than silently leaving the reader set one short.

    **What counts as a SQL-script literal is `_sql_script_literals`' business, and it is the whole
    reason this test does not fire on documentation.** Until a review measured it, this docstring
    said arm 2 keyed on a literal being "nothing but" a transaction statement; `_statements` had
    already retired that rule, and the sentence survived it by a round.
    """
    _transactions()  # populates _TIED
    orphans = [
        f"{rel}:{line} ({mode or 'DEFERRED (bare BEGIN)'})"
        for rel, line, mode in _begin_statements_in_sql_literals()
        if (rel, line) not in _TIED
    ]
    assert not orphans, (
        "these SQL-script literals carry transaction statements the ast arm did not tie "
        f"to an execute call: {orphans}. Either they reach SQLite by a route this file cannot "
        "see — in which case the reader census above is incomplete and the SPEC's uniqueness "
        "claim is unbound again — or they are dead. Resolve which; do not widen the exemption."
    )


# --------------------------------------------------------------------------------------
# The claims, parsed out of the paragraph and driven against the code
# --------------------------------------------------------------------------------------


def test_the_only_read_transaction_the_paragraph_claims_is_the_only_one_that_exists() -> None:
    """D81's open leg. The symbol, mode and module are read out of the sentence; the reader set is
    derived from `src/`; a second read transaction anywhere fails by `file:line`.

    This is the assertion whose absence kept D81 `PARTLY ADDRESSED`. Before it, adding a second
    `BEGIN DEFERRED` under `src/` falsified `docs/SPEC.md` with the whole suite green.
    """
    claim = _claim(_ONLY_READ, "which read transaction is the tree's only one")
    symbol = claim.group("symbol")
    module_path = claim.group("module")
    mode = _normalise(claim.group("mode")).upper().removeprefix("BEGIN").strip()
    _, spec_line, _ = _spec_block()

    readers, _ = _transactions()
    expected_rel = (_PKG / module_path).relative_to(_ROOT).as_posix()
    sites = [f"{rel}:{line} in {func}() ({m or 'bare BEGIN'})" for rel, line, func, m in readers]
    assert len(readers) == 1, (
        f"docs/SPEC.md:{spec_line} claims the tree's only read transaction is `{symbol}`'s, and "
        f"{len(readers)} executed read transactions exist under src/: {sites}. Either the SPEC "
        f"sentence is now false — a held read transaction defeats `wal_autocheckpoint` entirely "
        f"(D81 measured 8.12x WAL growth) — or it must be corrected in the same change that adds "
        f"the transaction. Do not relax this assertion to make the tree pass it."
    )
    rel, line, func, found_mode = readers[0]
    assert (rel, func, found_mode) == (expected_rel, symbol, mode), (
        f"docs/SPEC.md:{spec_line} names `{symbol}`'s `BEGIN {mode}` in `{module_path}`; the one "
        f"read transaction under src/ is at {rel}:{line} in {func}() with mode "
        f"{found_mode or 'bare BEGIN'}. The sentence and the code have drifted apart; fix "
        f"whichever is wrong, in one change."
    )


def test_the_symbol_the_paragraph_cites_exists_on_the_module_it_names() -> None:
    """The cited symbol is resolved on the imported module, so a rename fails here rather than
    leaving `docs/SPEC.md` pointing at a name the tree stopped using."""
    claim = _claim(_ONLY_READ, "which read transaction is the tree's only one")
    module = _resolve(claim.group("module"))
    _, spec_line, _ = _spec_block()
    assert hasattr(module, claim.group("symbol")), (
        f"docs/SPEC.md:{spec_line} cites `{claim.group('symbol')}` in "
        f"`{claim.group('module')}`; the module has no such attribute."
    )


def test_the_read_transaction_is_terminated_in_the_function_that_opens_it() -> None:
    """The property that makes the paragraph's caller enumeration non-load-bearing.

    The sentence bounds the hazard by naming `project_once`'s handle and the <= 1 Hz debounce.
    `build_state` has more than one caller (`_RESIDUAL` item 4 records the measurement), so the
    enumeration is not what makes the bound hold — this is: the `BEGIN` and its `COMMIT`/
    `ROLLBACK` are in the same function body, so no caller can hold the snapshot past the
    function's return, whatever the caller does afterwards. Moving the `BEGIN` up into a caller
    is exactly the edit that would re-open the hazard while leaving the reader *count* at one, so
    the count assertion above cannot see it and this one can.
    """
    claim = _claim(_ONLY_READ, "which read transaction is the tree's only one")
    symbol = claim.group("symbol")
    readers, _ = _transactions()
    # The site the paragraph names, not merely the first one found: this test and the count
    # assertion above must be independent discriminators, so a second read transaction added
    # elsewhere reddens that one alone and a lost terminator here reddens this one alone.
    named = [entry for entry in readers if entry[2] == symbol]
    assert named, (
        f"no executed read transaction is inside `{symbol}()`, which docs/SPEC.md names as the "
        f"tree's only one; found {[(rel, line, func) for rel, line, func, _ in readers]}"
    )
    rel, line, func, _ = named[0]
    tree = ast.parse((_ROOT / rel).read_text(encoding="utf-8"))
    body = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func
        ),
        None,
    )
    assert body is not None, f"{rel}:{line} - no function named {func}() to read"

    # WHICH OBJECT. The first version of this test collected `commit`/`rollback` calls by NAME
    # anywhere in the body and never looked at the receiver, so moving both terminators onto a
    # different object left the `BEGIN` on `conn` unterminated with all nine cases green -- the
    # test's own name false while it passed, which is the Rule 12 gap this project has measured
    # before. It is load-bearing here specifically: this test is what stands in for the SPEC's
    # caller enumeration (`_RESIDUAL` item 4), so a false one takes that substitution with it.
    opener = next(
        (
            node
            for node in ast.walk(body)
            if isinstance(node, ast.Call)
            and _callee_name(node.func) in _EXECUTE_CALLS
            and node.args
            and any(
                _BEGIN.match(statement)
                for statement in _statements(_literal_of(node.args[0], tree))
            )
        ),
        None,
    )
    assert opener is not None, (
        f"{rel}:{func}() no longer opens its read transaction through a resolvable literal, so "
        f"this test cannot tell which object holds it. That is an unresolvable-structure failure "
        f"on correct code and it says so rather than passing: re-anchor it, do not delete it."
    )
    assert isinstance(opener.func, ast.Attribute), (
        f"{rel}:{func}() opens its read transaction through a bare call, so there is no "
        f"receiver to compare terminators against."
    )
    holder = ast.unparse(opener.func.value)
    terminators = {
        _callee_name(node.func)
        for node in ast.walk(body)
        if isinstance(node, ast.Call)
        and _callee_name(node.func) in {"commit", "rollback"}
        and isinstance(node.func, ast.Attribute)
        and ast.unparse(node.func.value) == holder
    }
    elsewhere = sorted(
        f"{_callee_name(node.func)} on {ast.unparse(node.func.value)}"
        for node in ast.walk(body)
        if isinstance(node, ast.Call)
        and _callee_name(node.func) in {"commit", "rollback"}
        and isinstance(node.func, ast.Attribute)
        and ast.unparse(node.func.value) != holder
    )
    assert terminators == {"commit", "rollback"}, (
        f"{rel}:{func}() opens a read transaction on `{holder}` at line {line} but calls "
        f"{sorted(terminators) or 'neither commit nor rollback'} ON `{holder}`"
        + (f" (it does call {elsewhere}, on other objects)" if elsewhere else "")
        + ". Both are required, and both must be on the object that holds the transaction: "
        "without `commit` the snapshot outlives the function on every path, without `rollback` "
        "it outlives it on the exception path, and terminating some *other* connection ends no "
        "transaction at all. `docs/SPEC.md`'s 'no code path may hold a read transaction open "
        "across a wave' rests on this and on nothing else."
    )


def test_the_wal_bound_the_paragraph_names_is_the_value_the_connections_carry() -> None:
    """The pragma value is parsed out of the prose and compared to the live constant.

    `tests/test_db.py` already asserts `wal_autocheckpoint == 1000` on a real read handle and a
    real write handle, which is the stronger check of the *code*; it is deliberately not repeated
    here. What was missing is the other direction: nothing tied the SPEC's stated number to that
    value, so editing the paragraph to say 5 000 changed nothing and failed nothing.
    """
    claim = _claim(_AUTOCHECKPOINT, "the WAL's only bound")
    stated = int(claim.group("value"))
    module = _resolve(claim.group("module"))
    _, spec_line, _ = _spec_block()
    const_name = claim.group("const")
    assert hasattr(module, const_name), (
        f"docs/SPEC.md:{spec_line} says `{claim.group('module')}`'s `{const_name}` carries the "
        f"WAL bound; the module has no such attribute."
    )
    pragmas = getattr(module, const_name)
    values = [
        int(m.group(1))
        for m in (re.search(r"wal_autocheckpoint\s*=\s*(\d+)", str(p)) for p in pragmas)
        if m is not None
    ]
    assert values == [stated], (
        f"docs/SPEC.md:{spec_line} states `PRAGMA wal_autocheckpoint = {stated}` as the WAL's "
        f"only bound; `{claim.group('module')}`'s `{const_name}` sets {values}. A paragraph "
        f"naming a bound the connections do not carry is how the sentence this file replaced "
        f"came to credit a pragma no code issues."
    )


def test_the_paragraphs_denial_of_wal_checkpoint_still_holds_over_src() -> None:
    """The correction's own denial, with its scope parsed out of the sentence.

    `4398358` replaced a false attribution with an explicit denial — "nothing under `src/` issues
    `PRAGMA wal_checkpoint`". A count-based sweep cannot tell that denial from the claim it
    retired, which D81's marker records; this reads the *code* instead, so the denial is checked
    against what `src/` executes rather than against how often the phrase appears.
    """
    claim = _claim(_DENIAL, "that nothing under src/ issues PRAGMA wal_checkpoint")
    scope = claim.group("scope").rstrip("/")
    pragma = claim.group("pragma").lower()
    _, spec_line, _ = _spec_block()
    root = _ROOT / scope
    assert root.is_dir(), (
        f"docs/SPEC.md:{spec_line} scopes its denial to `{scope}/`, which is not a directory"
    )

    offenders: list[str] = []
    probe = re.compile(rf"\b{re.escape(pragma)}\b", re.I)
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        consts = _module_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if _callee_name(node.func) not in _EXECUTE_CALLS:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                sql = first.value
            elif isinstance(first, ast.Name) and first.id in consts:
                sql = consts[first.id][0]
            else:
                sql = None
            if sql is not None and probe.search(sql):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        f"docs/SPEC.md:{spec_line} denies that anything under `{scope}/` issues "
        f"`PRAGMA {pragma}`, and these do: {offenders}. If the checkpoint was added "
        f"deliberately, the paragraph is now false and must be corrected in the same change."
    )


def test_the_debounce_rate_the_paragraph_states_is_the_interval_the_code_uses() -> None:
    """The rate and the constant are read out of the sentence; the interval is read off the
    imported module. `<= N Hz` is a bound on frequency, so the interval must be at least `1/N`.
    Editing the prose to `<= 10 Hz` therefore changes what is asserted and fails.
    """
    claim = _claim(_DEBOUNCE, "the projection debounce rate")
    hz = float(claim.group("hz"))
    onlyread = _claim(_ONLY_READ, "which read transaction is the tree's only one")
    module = _resolve(onlyread.group("module"))
    _, spec_line, _ = _spec_block()
    const_name = claim.group("const")
    assert hasattr(module, const_name), (
        f"docs/SPEC.md:{spec_line} names `{const_name}` as the debounce; "
        f"`{onlyread.group('module')}` has no such attribute."
    )
    interval = float(getattr(module, const_name))
    assert hz > 0 and interval >= 1.0 / hz, (
        f"docs/SPEC.md:{spec_line} states the rebuild is debounced to at most {hz} Hz, which "
        f"requires `{const_name}` >= {1.0 / hz}s; it is {interval}s. At {1.0 / interval} Hz the "
        f"read transaction re-opens more often than the paragraph claims."
    )


#: The retired-name claim, as the docstring of `_begin_statements_in_sql_literals` states it. The
#: name is parsed OUT of the prose rather than written here, so editing the prose changes what is
#: asserted -- Guardrail 7's shape, applied to this module's own history.
_RETIRED_NAME = re.compile(r"\*\*Retired name:\s*`(?P<name>\w+)`\*\*")


def test_the_retired_arm_two_name_stays_retired() -> None:
    """The retirement sentence drives an assertion instead of merely recording a fact.

    `_whole_literal_begins` named a rule -- "the literal is nothing but a `BEGIN` statement" --
    that `_statements` retired when it began splitting scripts into fragments. The name outlived
    the rule by a round and the stale name is what a reconciler would have restored the defect
    from, so the rename was made and a sentence was written recording it.

    A review then measured that the sentence asserted **nothing**: deleting it left nine cases
    passing. This closes that. The retired name is read out of the prose, and this module must
    neither define nor call it; the sentence itself must survive, because a parse that finds no
    claim fails loudly here rather than passing over an absent one.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    claim = _RETIRED_NAME.search(_begin_statements_in_sql_literals.__doc__ or "")
    assert claim is not None, (
        "`_begin_statements_in_sql_literals` no longer states which name it retired, so nothing "
        "here can keep that name retired. If the rename is now ancient history and the sentence "
        "was deliberately dropped, drop this test in the same change -- do not leave it parsing "
        "an absent claim and passing vacuously."
    )
    retired = claim.group("name")
    tree = ast.parse(source)
    offenders = [
        f"{Path(__file__).name}:{node.lineno}"
        for node in ast.walk(tree)
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == retired
        )
        or (isinstance(node, ast.Call) and _callee_name(node.func) == retired)
    ]
    assert not offenders, (
        f"`{retired}` is retired -- it names the pre-`_statements` rule that a literal must be "
        f"nothing but a `BEGIN` statement -- and this module defines or calls it at {offenders}. "
        f"The current function is `_begin_statements_in_sql_literals`, gated by "
        f"`_sql_script_literals`. Reviving the old name revives the rule it describes."
    )


def test_the_residual_is_recorded_rather_than_implied_closed() -> None:
    """CLAUDE.md's stop rule: an honest disclosure beats a mechanism that looks enforced. The
    fourteen things this file cannot catch -- and, since a review found that direction
    missing entirely,
    the two it wrongly *can* catch -- are enumerated in `_RESIDUAL`, and this asserts they stay
    enumerated — a residual quietly deleted is how a partial binding starts reading as closure."""
    items = re.findall(r"^\d+\. ", _RESIDUAL, flags=re.MULTILINE)
    assert len(items) == 14, (
        f"_RESIDUAL enumerates {len(items)} gaps, expected 14. If a gap was genuinely closed, say "
        f"which test closed it in the same change; if one was added, update this count."
    )
    # Normalised, because a line-oriented substring check certifies a class as fixed when it is
    # not -- and did, here, in this file's own self-check: rewording item 2 wrapped the phrase
    # "not a mechanism" across a newline and the raw `in` test read a present sentence as deleted.
    # The whole file normalises before matching for exactly this reason; the check that enforces
    # the discipline had been exempt from it.
    prose = _normalise(_RESIDUAL)
    for phrase in ("not a mechanism", "reported rather than edited"):
        assert phrase in prose, (
            f"_RESIDUAL no longer says {phrase!r}. These two phrases are load-bearing: the first "
            f"is the disclaimer separating a mitigation from an enforced property, the second is "
            f"item 4's statement that the SPEC's caller enumeration was reported to the "
            f"orchestrator and not silently edited. Do not drop either to make this pass."
        )
