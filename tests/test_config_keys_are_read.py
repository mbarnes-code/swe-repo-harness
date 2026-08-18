"""Every §9 config key an operator can set must be read by some code.

`src/fleet/cli.py` already refuses `--context-policy` at the flag layer, and says why: the value
"would be parsed and then ignored by every rung", because `workers/base.context_policy_for_attempt`
"reads no config". `--stub-blocked` and `--no-anchoring-guard` are refused in the same block for
the same reason — *a flag that parses and is then dropped is read by the operator as honoured.*

That refusal covers the flag layer only. The identical defect one layer down — a **config key**
behind the same missing code — is still accepted silently: `config/fleet.yaml` validates, the
digest is stable, `fleet config` prints the operator's value back at them, and nothing ever reads
it. An inert key is worse than a missing feature, because it reports the feature as configured.
`llm.failover.open_after_failures: 10` looks like a tuned circuit breaker and is a comment.

So this file generalises the CLI's intent to the config surface: **every leaf field of every
settings `Section` must have its name appear somewhere in `src/fleet/` outside `settings.py`.**

The ratchet turns both ways, and the second direction is the point:

* a **new** inert key fails `test_every_config_key_is_read` — you cannot add a knob that nothing
  reads without either wiring it up or writing down that you did not;
* every `KNOWN_INERT` entry must **still be inert** (`test_known_inert_keys_are_still_inert`) — so
  the day someone wires one up, this file fails until they delete the line. A stale allowlist is
  the exact failure mode this design exists to prevent: an allowlist that outlives its defects
  quietly re-authorises the next one.

What this test deliberately is NOT
----------------------------------
It is a **name scan**, not a dataflow proof, and it is calibrated to under-report rather than to
guess. `_sources()` blanks every comment and docstring (`_strip_comments_and_docstrings`, via
`tokenize` for comments and `ast` for docstrings — not a regex, because "is this string literal a
docstring" depends on its position in the tree, not its spelling) before any pattern is matched, so
prose can no longer masquerade as a read. Two consequences remain worth knowing before trusting a
pass:

1. A leaf whose bare field name collides with something unrelated is decided with a **qualified**
   match instead — `QUALIFIED_MATCH_KEYS` names the immediate parent field, and
   `_qualified_readers` requires `parent.leaf` (e.g. `failover.enabled`) rather than the bare leaf.
   This resolves the common-English-word case (`transform.ladder.role`/`.context_policy` pass on
   unrelated `role`s and `context_policy`s elsewhere) **and** the same-name-different-object case
   (`llm.max_schema_repairs`, `llm.failover.enabled`, `llm.failover.max_targets_per_call` collide
   with real, unrelated fields of `llm/client.py`'s `CallPolicy` — injected precisely so a run does
   not have to load config, per its own docstring — and, for `enabled`, with the genuinely-wired
   `scan.contracts.enabled`). All five are true inert keys once qualified, live in `KNOWN_INERT`.
2. `UNVERIFIABLE` is what is left when even the qualified match cannot decide it:
   `transform.ladder.tier` stays undecidable because `orchestrator/runner.py:655` calls
   `ladder.tier(attempt)` where `ladder` is `orchestrator/retry.py`'s `LadderState` — a *different*
   object with its own `tier` **method**, built from `workers/base.TIER_LADDER`, not
   `config.transform.ladder`. `LadderState(...)` at `runner.py:428` takes no `ladder=`, so the
   configured ladder never reaches a rung, but the text `ladder.tier` is genuinely there, naming
   genuinely unrelated code — no textual scan, qualified or not, tells "attribute of a config
   model" from "method of an unrelated same-named object" apart.
   `test_unverifiable_keys_still_defeat_the_scan` holds each member to *staying* undecided: the day
   either check starts reporting it inert, this file fails until the key is retriaged into
   `KNOWN_INERT` with the evidence that finally decided it.

Reads that live *inside* `settings.py` are invisible by construction — that exclusion is what stops
every key trivially matching its own declaration. `DECLARATIVE` is where the genuine ones go, and
every member carries the line that reads it.

None of that weakens the guarantee in the failing direction: a key this test calls inert is inert.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
import typing
from functools import cache
from pathlib import Path

import pytest
from pydantic import BaseModel

from fleet import settings as settings_mod
from fleet.settings import FleetConfig, ModelsConfig, ReposManifest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "fleet"
SETTINGS_FILE = SRC / "settings.py"

ROOTS: tuple[tuple[str, type[BaseModel]], ...] = (
    ("fleet.yaml", FleetConfig),
    ("repos.yaml", ReposManifest),
    ("models.yaml", ModelsConfig),
)
"""The three operator-editable §9 documents. Keys are qualified by the file that carries them
(`fleet.yaml:llm.cache_path`) because `version` means one thing in `repos.yaml` and another in
`models.yaml`, and a bare-name set would silently merge them."""


# --------------------------------------------------------------------------------------
# the allowlists
# --------------------------------------------------------------------------------------

KNOWN_INERT: frozenset[str] = frozenset(
    {
        # Ledger entries pending for every line below — no D-number is invented here.
        #
        # --- §11.8 backpressure: the whole block, machinery and all -----------------------
        # `rate_limit` / `aimd` / `honor_retry_after` / `rpm` / `tpm` appear at their
        # declaration and nowhere else in the tree: no limiter, no token bucket, no
        # `Retry-After` handler. `llm/client.py` retries transients on a fixed backoff.
        "fleet.yaml:llm.rate_limit",                       # settings.py:681 — section unreferenced
        "fleet.yaml:llm.rate_limit.honor_retry_after",     # settings.py:651
        "fleet.yaml:llm.rate_limit.defaults.rpm",          # settings.py:635
        "fleet.yaml:llm.rate_limit.defaults.tpm",          # settings.py:636
        "fleet.yaml:llm.rate_limit.targets.rpm",           # settings.py:635 (same RateLimitEntry)
        "fleet.yaml:llm.rate_limit.targets.tpm",           # settings.py:636
        "fleet.yaml:llm.rate_limit.aimd",                  # settings.py:654 — section unreferenced
        "fleet.yaml:llm.rate_limit.aimd.shrink_factor",    # settings.py:642
        "fleet.yaml:llm.rate_limit.aimd.grow_every_s",     # settings.py:643
        # `aimd.floor` is NOT listed: it passes only because "floor" is a common word, not
        # because anything reads it. See limitation 2 in the module docstring.
        #
        # --- §11.8 failover: the circuit breaker's three tuning keys ----------------------
        # `llm/client.py` walks `max_targets_per_call` targets and honours `failover.enabled`,
        # but opens no circuit: nothing counts failures, nothing cools down, nothing halts.
        "fleet.yaml:llm.failover.open_after_failures",     # settings.py:661
        "fleet.yaml:llm.failover.cooldown_s",              # settings.py:662
        "fleet.yaml:llm.failover.on_tier_exhausted",       # settings.py:664
        #
        # --- ADR-0021 anti-anchoring: §3.2 step 5 has no implementation -------------------
        # `models/tasks.py:211` declares `TransformTask.reasks` and nothing ever increments it,
        # so neither the per-rung cap nor its exhaustion policy has a counter to act on. Same
        # missing machinery `cli.py` cites when it refuses `--no-anchoring-guard`.
        "fleet.yaml:transform.anchoring.max_reasks_per_rung",  # settings.py:427
        "fleet.yaml:transform.anchoring.on_exhausted",         # settings.py:428
        #
        # --- §3.1 the native baseline build/test gate -------------------------------------
        # The `BaselineBuild` section name appears nowhere outside its declaration; its two
        # leaves (`enabled`, `timeout_s`) pass only on unrelated matches of those words.
        "fleet.yaml:preflight.baseline_build",             # settings.py:287
        #
        # --- timeouts and ceilings with no consumer ---------------------------------------
        "fleet.yaml:budgets.build_timeout_s",              # settings.py:267
        "fleet.yaml:budgets.clone_timeout_s",              # settings.py:268
        "fleet.yaml:graph.max_edges",                      # settings.py:411
        "fleet.yaml:run.reaper_interval_s",                # settings.py:215
        "fleet.yaml:run.projection_hz",                    # settings.py:217
        #
        # --- single-value policy keys nothing branches on ---------------------------------
        "fleet.yaml:stubs.on_budget_exhausted",            # settings.py:604
        "fleet.yaml:build.fail_on_missing_adapter",        # settings.py:579
        "fleet.yaml:build.openapi_generator",              # settings.py:580
        "fleet.yaml:scan.unknown_ecosystem_dest",          # settings.py:364
        "fleet.yaml:pr.reviewers_from",                    # settings.py:692
        "fleet.yaml:llm.cache_path",                       # settings.py:679 — `llm/cache.py`
        #                                                    takes its path from the caller
        #
        # --- revealed by comment/docstring stripping (was passing on prose alone) --------
        # `orchestrator/budgets.py:975`'s docstring names it; `Limits.create`'s `llm_overrides=`
        # kwarg is never passed by any of its five call sites (`cli.py:1812,4066,7488,7560,9107`).
        # `settings.py:1253`'s `llm_concurrency` accessor DOES apply it correctly — but nothing
        # outside `settings.py` calls `llm_concurrency` either (only `tests/test_settings.py`).
        "fleet.yaml:llm.concurrency_overrides",             # settings.py:683
        # The section name itself: every real (non-comment) occurrence of "failover" in the tree
        # is `llm/client.py`'s own module reference to a sibling file that does not exist as such
        # (`failover.py`, named in a comment). `.enabled` and `.max_targets_per_call` below are
        # true leaves of this same section, decided separately via qualified match.
        "fleet.yaml:llm.failover",                          # settings.py:682
        # `validate_memory_budget` (settings.py:1272) is the only reader and is never called —
        # `memory_commitment_mb`'s own docstring says the caller "decides"; none does.
        "fleet.yaml:budgets.max_host_rss_mb",               # settings.py:265
        # Only a comment (`workers/prwriter.py:274`) and a docstring (`cli.py:9027`) cite it as
        # the rationale for HELD; no deadline is ever checked against it.
        "fleet.yaml:pr.merge_wait_timeout_s",               # settings.py:694
        # `obs/redact.py:72-73` hard-codes `ENTROPY_MIN_BITS = 4.0` / `ENTROPY_MIN_LEN = 20` —
        # the same defaults as the config, but as module constants, not reads. The `#:` comment
        # above them just cross-references the config keys it does not consult.
        "fleet.yaml:redaction.entropy_min_bits",            # settings.py:307
        "fleet.yaml:redaction.entropy_min_len",             # settings.py:308
        #
        # --- generic-word / same-name-different-object collisions, decided via qualified match --
        # `transform.ladder[i].role` and `.context_policy`: `.tier` is their sibling and stays
        # genuinely undecidable (see `UNVERIFIABLE`); these two ARE decidable — `ladder.role` and
        # `ladder.context_policy` occur nowhere for real once qualified.
        "fleet.yaml:transform.ladder.role",                 # settings.py:419
        "fleet.yaml:transform.ladder.context_policy",       # settings.py:420
        # `llm/client.py`'s `CallPolicy` (its own docstring: "Injected, so a test does not have to
        # load config and a run does not have to hard-code a default") declares fields with the
        # SAME names as these three config leaves. `CallPolicy()` is constructed with no override
        # at its one call site (`client.py:478`): `RunContext.llm_policy` is declared and consumed
        # (`orchestrator/context.py:140,152`) but never assigned by any of the five `RunContext(`
        # sites in `cli.py` (1806, 4074, 7496, 7568, 9115). The bare names pass the plain scan on
        # `self._policy.max_schema_repairs` / `.max_targets_per_call` — real code, wrong object.
        "fleet.yaml:llm.max_schema_repairs",                # settings.py:680
        "fleet.yaml:llm.failover.enabled",                  # settings.py:660 — also collides
        #                                                      with the genuinely-wired
        #                                                      `scan.contracts.enabled`
        "fleet.yaml:llm.failover.max_targets_per_call",     # settings.py:663
    }
)

QUALIFIED_MATCH_KEYS: frozenset[str] = frozenset(
    {
        # These five leaves are read by the plain bare-name scan (`_readers`) for reasons that
        # have nothing to do with the config: a common English word, or a same-named field on an
        # unrelated class. `_inert_keys()` decides them with `_qualified_readers` instead — the
        # immediate parent field name plus the leaf (`failover.enabled`, `llm.max_schema_repairs`)
        # — which does not occur for real in any of the five. See the `KNOWN_INERT` comments above
        # for the specific collision each one resolves.
        "fleet.yaml:transform.ladder.role",
        "fleet.yaml:transform.ladder.context_policy",
        "fleet.yaml:llm.max_schema_repairs",
        "fleet.yaml:llm.failover.enabled",
        "fleet.yaml:llm.failover.max_targets_per_call",
    }
)

UNVERIFIABLE: frozenset[str] = frozenset(
    {
        # Not prose (stripped) and not a generic word resolved by qualification: the text
        # `ladder.tier` genuinely occurs in real, non-comment code at `orchestrator/runner.py:655`
        # — `ladder.tier(attempt)` — but `ladder` there is `orchestrator/retry.py`'s `LadderState`
        # (`retry.py:79`), whose OWN `tier` is a method (`retry.py:95`) derived from
        # `workers/base.TIER_LADDER`, not from `config.transform.ladder`. `LadderState(...)` at
        # `runner.py:428` takes no `ladder=` kwarg, so the configured ladder never reaches a rung
        # — but "attribute of a Pydantic model" vs. "method of an unrelated same-named object" is
        # not a distinction a text scan, qualified or bare, can draw. Its siblings `.role` and
        # `.context_policy` ARE decided (see `QUALIFIED_MATCH_KEYS`) — this is the genuine
        # remainder.
        "fleet.yaml:transform.ladder.tier",                 # settings.py:418
    }
)
"""Known-inert leaves this test cannot itself verify as inert — not a second amnesty list. Every
member must keep failing BOTH the bare scan and the qualified one
(`test_unverifiable_keys_still_defeat_the_scan`); the moment either check starts reporting a member
inert, that member has become decidable and this file fails until it is retriaged into
`KNOWN_INERT` with the evidence that finally decided it."""

DECLARATIVE: frozenset[str] = frozenset(
    {
        # NOT an amnesty list. Each key here IS read — by `settings.py` itself, through an
        # accessor or a startup validator whose result is what the rest of the tree consumes.
        # The scan cannot see those reads because it excludes `settings.py`, which is the same
        # exclusion that stops every key matching its own declaration.
        # `test_declarative_keys_are_read_inside_settings` holds them to that claim.
        #
        # Read by `LlmConcurrency.for_tier` (settings.py:227-230), which
        # `orchestrator/budgets.py:980` calls to size the per-tier semaphore. Its siblings
        # `heavy` and `cheap` pass the scan only on unrelated matches of those words — this key
        # is not more inert than they are, it is merely spelled less commonly.
        "fleet.yaml:concurrency.llm.workhorse",            # settings.py:224
        # Read by the §9 rule-3 startup check (settings.py:1410-1420): a tier whose first target
        # declares a smaller `max_context` is a `ConfigValidationError` at load. Enforced in
        # full; the enforcement simply lives in the settings module.
        "fleet.yaml:llm.require_capabilities",             # settings.py:684
        "fleet.yaml:llm.require_capabilities.min_context",  # settings.py:671
    }
)


# --------------------------------------------------------------------------------------
# the walk and the scan
# --------------------------------------------------------------------------------------


def _nested_settings_models(annotation: object) -> tuple[type[BaseModel], ...]:
    """Settings models reachable from one annotation, including through `tuple[...]`/`dict[...]`.

    Only models declared in `fleet.settings` are followed. A model from elsewhere (a
    `models/tasks.py` `BackendTarget` under `models.yaml:profiles`) is left as a leaf on purpose:
    its fields would match their own declaration in that module and pass for free.
    """
    if isinstance(annotation, type):
        if issubclass(annotation, BaseModel) and annotation.__module__ == settings_mod.__name__:
            return (annotation,)
        return ()
    found: list[type[BaseModel]] = []
    for arg in typing.get_args(annotation):
        found.extend(_nested_settings_models(arg))
    return tuple(found)


def _walk(model: type[BaseModel], prefix: str, seen: frozenset[type[BaseModel]]) -> dict[str, str]:
    """Qualified key → the Python field name a reader would have to spell to use it.

    A field holding nested models yields BOTH its own name and its children: the block name is
    the evidence that the *section* is wired up at all, and `preflight.baseline_build` is exactly
    the case that needs it — a section nothing constructs, whose leaves are named `enabled` and
    `timeout_s` and so match half the tree.
    """
    keys: dict[str, str] = {}
    for name, field in model.model_fields.items():
        qualified = f"{prefix}{name}"
        keys[qualified] = name
        for nested in _nested_settings_models(field.annotation):
            if nested in seen:  # pragma: no cover - the §9 models are acyclic today
                continue
            keys.update(_walk(nested, f"{qualified}.", seen | {nested}))
    return keys


@cache
def _config_keys() -> dict[str, str]:
    keys: dict[str, str] = {}
    for filename, root in ROOTS:
        keys.update(_walk(root, f"{filename}:", frozenset({root})))
    return keys


def _blank_span(lines: list[str], start: tuple[int, int], end: tuple[int, int]) -> None:
    """Replace `lines[start:end]` (1-indexed line, 0-indexed column, `tokenize`/`ast` convention)
    with spaces in place, preserving every newline so line numbers and file length are untouched —
    only the ability to match an identifier inside the span is destroyed."""

    def blanked(text: str) -> str:
        if text.endswith("\n"):
            return " " * (len(text) - 1) + "\n"
        return " " * len(text)

    (start_line, start_col), (end_line, end_col) = start, end
    if start_line == end_line:
        line = lines[start_line - 1]
        lines[start_line - 1] = line[:start_col] + blanked(line[start_col:end_col]) + line[end_col:]
        return
    first = lines[start_line - 1]
    lines[start_line - 1] = first[:start_col] + blanked(first[start_col:])
    for i in range(start_line, end_line - 1):
        lines[i] = blanked(lines[i])
    last = lines[end_line - 1]
    lines[end_line - 1] = blanked(last[:end_col]) + last[end_col:]


def _strip_comments_and_docstrings(source: str) -> str:
    """Blank every `#` comment and every module/class/function docstring, so a config key name
    that appears only in prose can no longer be mistaken for a read.

    `tokenize` finds comments (a `COMMENT` token, unambiguous). `ast` finds docstrings: a string
    literal is a docstring only by *position* — the first statement of a module, class, or
    function body — which a regex cannot decide (it cannot tell that string apart from any other
    string literal without parsing the surrounding structure), so this is deliberately AST-driven,
    not pattern-matched.
    """
    lines = source.splitlines(keepends=True)

    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            _blank_span(lines, tok.start, tok.end)

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            value = first.value
            assert value.end_lineno is not None and value.end_col_offset is not None
            _blank_span(
                lines,
                (value.lineno, value.col_offset),
                (value.end_lineno, value.end_col_offset),
            )

    return "".join(lines)


@cache
def _sources() -> tuple[tuple[Path, str], ...]:
    """Every `src/fleet/` module except `settings.py`, comments and docstrings blanked, read as
    text.

    A plain source scan, not import-and-introspect: the whole point is to catch a key that *no
    object anywhere references*, and an importing test can only ever see what was imported. The
    stripping keeps that scan from being fooled by prose that merely names a key.
    """
    return tuple(
        (path, _strip_comments_and_docstrings(path.read_text(encoding="utf-8")))
        for path in sorted(SRC.rglob("*.py"))
        if path != SETTINGS_FILE and "__pycache__" not in path.parts
    )


def _readers(field_name: str) -> list[str]:
    pattern = re.compile(rf"\b{re.escape(field_name)}\b")
    return [
        str(path.relative_to(SRC)) for path, text in _sources() if pattern.search(text)
    ]


def _qualified_readers(parent: str, field_name: str) -> list[str]:
    """Like `_readers`, but requires the immediate parent field name immediately before the leaf
    (`failover.enabled`, `llm.max_schema_repairs`) rather than the bare leaf alone. Used only for
    `QUALIFIED_MATCH_KEYS`, where the bare name collides with a common word or an unrelated
    same-named field and the qualified pair does not."""
    pattern = re.compile(rf"\b{re.escape(parent)}\s*\.\s*{re.escape(field_name)}\b")
    return [
        str(path.relative_to(SRC)) for path, text in _sources() if pattern.search(text)
    ]


def _parent_field(qualified: str) -> str:
    """`fleet.yaml:llm.failover.enabled` -> `failover` — the immediate parent field name."""
    dotted = qualified.split(":", 1)[1]
    segments = dotted.split(".")
    assert len(segments) >= 2, f"{qualified!r} has no parent to qualify against"
    return segments[-2]


@cache
def _inert_keys() -> frozenset[str]:
    inert: set[str] = set()
    for qualified, name in _config_keys().items():
        if qualified in QUALIFIED_MATCH_KEYS:
            if not _qualified_readers(_parent_field(qualified), name):
                inert.add(qualified)
        elif not _readers(name):
            inert.add(qualified)
    return frozenset(inert)


# --------------------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------------------


def test_the_scan_sees_a_real_config_surface() -> None:
    """Guard the guard: a walk that silently yields nothing would pass every other test here.

    The exact count (180, at time of writing) is a tripwire on its own: `len(keys) > 100` would
    still pass if an entire section vanished from the walk (`verify` alone is 8 keys), so the
    per-section coverage loop below is the one that actually catches that regression — the exact
    count just makes any drift, section-sized or not, visible instead of silently tolerated.
    """
    keys = _config_keys()
    assert len(keys) == 180, (
        f"walked {len(keys)} keys, expected 180 — recount deliberately (a key was added/removed, "
        "or a whole section was silently dropped from the walk) and update this number"
    )
    for filename, root in ROOTS:
        for section_name in root.model_fields:
            prefix = f"{filename}:{section_name}"
            assert any(k == prefix or k.startswith(f"{prefix}.") for k in keys), (
                f"no key found for top-level section {prefix!r} — it was dropped from the walk"
            )
    assert "fleet.yaml:run.monorepo_path" in keys
    assert "fleet.yaml:transform.ladder.tier" in keys, "tuple[LadderRung, ...] must be recursed"
    assert "fleet.yaml:llm.rate_limit.targets.rpm" in keys, "dict[str, RateLimitEntry] likewise"
    assert "models.yaml:default_profile" in keys
    assert _readers("monorepo_path"), "the source scan itself found nothing — check SRC"


def test_every_config_key_is_read() -> None:
    """A key an operator can set is a key some code reads.

    Anything else is a knob wired to nothing: it validates, it digests, `fleet config` echoes it
    back, and the run behaves as if it were never written. New offenders fail here.
    """
    unexplained = sorted(_inert_keys() - KNOWN_INERT - DECLARATIVE - UNVERIFIABLE)
    assert not unexplained, (
        "these config keys are never named in src/fleet/ outside settings.py, so an operator who "
        "sets them changes nothing:\n  "
        + "\n  ".join(unexplained)
        + "\nWire the key up, or add it to KNOWN_INERT with its settings.py line and a note that "
        "the ledger entry is pending."
    )


@pytest.mark.parametrize("key", sorted(KNOWN_INERT))
def test_known_inert_keys_are_still_inert(key: str) -> None:
    """The other half of the ratchet — the half that keeps the allowlist honest.

    When the missing code lands, the key stops being inert and this fails until the line is
    deleted. Without it the allowlist would outlive its defects and quietly grant amnesty to the
    next key that happens to share a name with one of these.
    """
    assert key in _config_keys(), f"{key} is not a config key any more; drop the KNOWN_INERT line"
    name = _config_keys()[key]
    readers = (
        _qualified_readers(_parent_field(key), name)
        if key in QUALIFIED_MATCH_KEYS
        else _readers(name)
    )
    assert key in _inert_keys(), (
        f"{key} is now read by {readers} — it is no longer inert. "
        "Remove it from KNOWN_INERT (and close its ledger entry)."
    )


@pytest.mark.parametrize("key", sorted(UNVERIFIABLE))
def test_unverifiable_keys_still_defeat_the_scan(key: str) -> None:
    """`UNVERIFIABLE` is not a second amnesty list.

    Membership means: the plain scan calls this key read, and the qualified `parent.leaf` scan
    ALSO calls it read — both wrong, both wrong for a reason no text scan can resolve (a same-named
    method or attribute on a genuinely different, unrelated object). If either check starts
    reporting it inert, the collision that made it undecidable is gone and this key has become
    decidable — it must move to `KNOWN_INERT` with the evidence that finally decided it, not stay
    here where it would otherwise sit unexamined forever.
    """
    assert key in _config_keys(), f"{key} is not a config key any more; drop the UNVERIFIABLE line"
    name = _config_keys()[key]
    assert key not in _inert_keys(), (
        f"{key} is now correctly flagged inert by the plain scan — it has become decidable. "
        "Move it to KNOWN_INERT with the evidence."
    )
    parent = _parent_field(key)
    qualified_hits = _qualified_readers(parent, name)
    assert qualified_hits, (
        f"{key} is now correctly flagged inert by the qualified `{parent}.{name}` check (no "
        f"collision found) — it has become decidable. Move it to KNOWN_INERT with the evidence."
    )


@pytest.mark.parametrize("key", sorted(DECLARATIVE))
def test_declarative_keys_are_read_inside_settings(key: str) -> None:
    """DECLARATIVE must never become a place to park a finding.

    A member earns its place by being read *somewhere in `settings.py` other than its own
    declaration*. One occurrence means the escape hatch is hiding an inert key.
    """
    assert key in _config_keys(), f"{key} is not a config key any more; drop the DECLARATIVE line"
    field_name = _config_keys()[key]
    occurrences = len(
        re.findall(rf"\b{re.escape(field_name)}\b", SETTINGS_FILE.read_text(encoding="utf-8"))
    )
    assert occurrences > 1, (
        f"{key} appears once in settings.py — its declaration — and nowhere in the rest of "
        "src/fleet/. That is an inert key, not a declarative one; move it to KNOWN_INERT."
    )


def test_the_allowlists_are_disjoint() -> None:
    """One key, one verdict: inert, read, or undecidable. A key in two sets is a claim in two
    directions at once."""
    assert not (KNOWN_INERT & DECLARATIVE)
    assert not (KNOWN_INERT & UNVERIFIABLE)
    assert not (DECLARATIVE & UNVERIFIABLE)


def test_qualified_match_keys_are_a_subset_of_known_inert() -> None:
    """`QUALIFIED_MATCH_KEYS` is an implementation detail of `_inert_keys()`, not its own verdict
    — every member must resolve to a real classification (here, `KNOWN_INERT`; none currently
    resolve to a plain read) or this set is silently steering keys nowhere."""
    assert QUALIFIED_MATCH_KEYS <= KNOWN_INERT
