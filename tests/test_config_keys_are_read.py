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
guess. Three consequences worth knowing before trusting a pass:

1. A name that appears only in a comment or a docstring counts as read. `llm.concurrency_overrides`
   passes on `orchestrator/budgets.py`'s docstring alone, even though `Limits.create`'s
   `llm_overrides=` parameter is never passed by any caller — the override is validated at startup
   (`settings.py:_check_concurrency_overrides`) and then never applied.
2. A leaf whose field name is a common word is unfalsifiable here. `transform.ladder[i].tier`,
   `.role` and `.context_policy` pass on unrelated occurrences of `tier`/`role`/`context_policy`,
   yet `orchestrator/runner.py` builds its `LadderState` with no `ladder=`, so `retry.py` keeps
   `DEFAULT_LADDER` and takes its tiers from the module constant `workers/base.TIER_LADDER`. The
   configured ladder never reaches a rung.
3. Reads that live *inside* `settings.py` are invisible by construction — that exclusion is what
   stops every key trivially matching its own declaration. `DECLARATIVE` is where the genuine ones
   go, and every member carries the line that reads it.

None of that weakens the guarantee in the failing direction: a key this test calls inert is inert.
"""

from __future__ import annotations

import re
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
    }
)

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


@cache
def _sources() -> tuple[tuple[Path, str], ...]:
    """Every `src/fleet/` module except `settings.py`, read as text.

    A plain source scan, not import-and-introspect: the whole point is to catch a key that *no
    object anywhere references*, and an importing test can only ever see what was imported.
    """
    return tuple(
        (path, path.read_text(encoding="utf-8"))
        for path in sorted(SRC.rglob("*.py"))
        if path != SETTINGS_FILE and "__pycache__" not in path.parts
    )


def _readers(field_name: str) -> list[str]:
    pattern = re.compile(rf"\b{re.escape(field_name)}\b")
    return [
        str(path.relative_to(SRC)) for path, text in _sources() if pattern.search(text)
    ]


@cache
def _inert_keys() -> frozenset[str]:
    return frozenset(
        qualified for qualified, name in _config_keys().items() if not _readers(name)
    )


# --------------------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------------------


def test_the_scan_sees_a_real_config_surface() -> None:
    """Guard the guard: a walk that silently yields nothing would pass every other test here."""
    keys = _config_keys()
    assert len(keys) > 100, f"only {len(keys)} keys walked; the §9 surface is far larger"
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
    unexplained = sorted(_inert_keys() - KNOWN_INERT - DECLARATIVE)
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
    assert key in _inert_keys(), (
        f"{key} is now read by {_readers(_config_keys()[key])} — it is no longer inert. "
        "Remove it from KNOWN_INERT (and close its ledger entry)."
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
    """One key, one verdict: inert or read. A key in both sets is a claim in two directions."""
    assert not (KNOWN_INERT & DECLARATIVE)
