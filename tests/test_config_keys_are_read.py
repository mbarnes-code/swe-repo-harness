"""Every §9 config key an operator can set must be read by some code.

**Corrected 2026-09-02 (round EE): `--context-policy` is no longer refused.** This module's
opening two paragraphs originally used `cli.py`'s then-blanket refusal of `--context-policy` as
the motivating example for the whole file's design — round EE wired `transform.ladder.
context_policy` through to the worker for real (`orchestrator/runner.py:523`), closing §12.35's
CLI-level proof, and the refusal was removed. `--stub-blocked` is still refused in the same block,
for the same reason — *a flag that parses and is then dropped is read by the operator as
honoured* — so the file's motivating pattern still holds for that one. `--no-anchoring-guard` is
ALSO no longer refused (round VI task 46, §12.36): it now sets `transform.anchoring.enabled:
false` for real. Two of the original example's three refusals are now historical.

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
guess. `_sources()` blanks every comment and every string literal (`_strip_comments_and_docstrings`,
via `tokenize` for comments and `ast` for string literals — not a regex, because telling a string
literal from surrounding code requires parsing) before any pattern is matched, so prose can no
longer masquerade as a read. This blanks docstrings, but also every OTHER string literal — a
`Field(description="...")` kwarg, an f-string, a dict key — because prose hides in all of those
just as easily; `models/state.py`'s `heartbeat_ttl_seconds` field described itself with a sentence
naming `stale_after_s`, which is not a docstring by position but is exactly the same hazard. Two
consequences remain worth knowing before trusting a pass:

1. A leaf whose bare field name collides with something unrelated is decided with a **qualified**
   match instead — `QUALIFIED_MATCH_KEYS` names the immediate parent field, and
   `_qualified_readers` requires `parent.leaf` (e.g. `failover.enabled`) rather than the bare leaf.
   This resolves the common-English-word case (`transform.ladder.role`/`.context_policy` pass on
   unrelated `role`s and `context_policy`s elsewhere) **and** the same-name-different-object case
   (`llm.max_schema_repairs`, `llm.failover.enabled`, `llm.failover.max_targets_per_call` collide
   with real, unrelated fields of `llm/client.py`'s `CallPolicy` — injected precisely so a run does
   not have to load config, per its own docstring — and, for `enabled`, with the genuinely-wired
   `scan.contracts.enabled`). The collisions are all still there, so all five still need the
   qualified check; their **verdicts** now differ. Only `transform.ladder.role` remains inert and
   stays in `KNOWN_INERT`. The three `llm.` ones are **read** as of 2026-08-22:
   `orchestrator/context.py::call_policy_for` maps them onto the `CallPolicy` that
   `RunContext.__post_init__` now injects. `transform.ladder.context_policy` is **read** as of
   2026-09-02 (round EE): `orchestrator/runner.py:523`'s `_drive` sources
   `ladder=tuple(rung.context_policy for rung in self.ctx.config.transform.ladder)` — so for all
   four, the qualified pair occurs for real and the `KNOWN_INERT` lines were deleted. They stay in
   `QUALIFIED_MATCH_KEYS` deliberately: the bare-name collision is what would let a *revert* of
   that wiring pass `test_every_config_key_is_read` unnoticed. `transform.ladder.context_policy`
   needed one more piece to make that protection real: its genuine reader spells the qualifying
   variable `rung`, not the `ladder` `_parent_field` derives from the dotted config path, a gap
   round EE's own final review found — closed via `_PARENT_FIELD_OVERRIDES` (see
   `_parent_field`'s definition), which maps this one key's parent explicitly rather than leaving
   it structurally unverifiable.
2. `UNVERIFIABLE` is what is left when even the qualified match cannot decide it:
   `transform.ladder.tier` stays undecidable because `orchestrator/runner.py:802` calls
   `ladder.tier(attempt)` where `ladder` is `orchestrator/retry.py`'s `LadderState` — a *different*
   object with its own `tier` **method**. **Corrected 2026-09-02 (round EE): the reasoning below
   this point used to be that `LadderState.tier` was built from the hardcoded
   `workers.base.TIER_LADDER` rather than config, and that `LadderState(...)` took no `ladder=`
   argument — both now false.** `LadderState.tier` (`orchestrator/retry.py:100`) now delegates to
   the parameterized `workers.base.tier_for_attempt(attempt, self.ladder)`, and `runner.py:512`'s
   `LadderState(...)` construction DOES pass `ladder=` — the configured ladder genuinely reaches
   `.tier()` now, same as `.context_policy()` already did. **The verdict is unchanged regardless**:
   `config.transform.ladder[i].tier` (the declared field on `LadderRung`) is still never read by
   anything — `.tier()`'s value is *derived* from `.context_policy` via `workers.base.
   _TIER_FOR_RUNG`, not read from the config object's own `.tier` field, so that field stays
   undecidable by a name scan for the same structural reason as before, just not the reason
   originally written down here. The text `ladder.tier` is genuinely there, naming
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
        # `aimd.floor` used to be left off this list on purpose, with a comment admitting the bare
        # scan passes it only because "floor" is a common word — a disclosed-but-unenforced hole
        # this file itself flagged and never closed. It is a real same-name-different-object
        # collision: `util/fs.py:87-96`'s `InsufficientDiskSpaceError.__init__(*, floor: int, ...)`
        # is the disk-space floor for `preflight.min_free_bytes`, unrelated to the AIMD backoff
        # floor. Qualified as `aimd.floor` below.
        "fleet.yaml:llm.rate_limit.aimd.floor",             # settings.py:644
        #
        # --- §11.8 failover: the circuit breaker's three tuning keys ----------------------
        # `orchestrator/context.py::call_policy_for` maps `failover.enabled` and
        # `failover.max_targets_per_call` onto the `CallPolicy` the client walks (2026-08-22),
        # but that opens no circuit: nothing counts failures, nothing cools down, nothing
        # halts. `CallPolicy` cannot express a per-target `BackendHealth`, and §11.8's
        # `llm/failover.py` does not exist, so these three stay inert.
        "fleet.yaml:llm.failover.open_after_failures",     # settings.py:661
        "fleet.yaml:llm.failover.cooldown_s",              # settings.py:662
        "fleet.yaml:llm.failover.on_tier_exhausted",       # settings.py:664
        #
        # --- ADR-0021 anti-anchoring: §3.2 step 5 -----------------------------------------
        # `max_reasks_per_rung`, the section itself, and `.enabled` are READ as of round VI
        # task 46 (§12.36): `rewrite/approach.py`'s `approach_signature` fingerprinting and
        # `workers/rewrite.py::RewriteWorker._repair_guarded` genuinely read
        # `payload.anchoring_enabled`/`payload.max_reasks_per_rung`, threaded from
        # `settings.config.transform.anchoring.{enabled,max_reasks_per_rung}` by
        # `cli._transform_payloads`/`cli._apply_anchoring_override` (the latter is what
        # `--no-anchoring-guard` now sets, in place of the refusal this file used to cite).
        # `.enabled` stays in `QUALIFIED_MATCH_KEYS` below (same reasoning `transform.ladder.
        # context_policy` documents): the bare leaf still collides with the genuinely-wired
        # `scan.contracts.enabled`, so only the QUALIFIED pair is evidence, and keeping it
        # qualified is what would catch a revert. `on_exhausted` (`advance`/`fail` at reask
        # exhaustion) is NOT wired — this task's guard always defers to the ladder's ordinary
        # rung-failure handling, which is `advance`-shaped by construction; `fail`'s "terminate
        # immediately even with a next rung available" branch has no code — so it alone stays
        # inert here.
        "fleet.yaml:transform.anchoring.on_exhausted",         # settings.py:428
        #
        # --- §3.1 the native baseline build/test gate -------------------------------------
        # The `BaselineBuild` section name appears nowhere outside its declaration; its two
        # leaves (`enabled`, `timeout_s`) pass the BARE scan only on unrelated matches of those
        # words — a comment on this very entry said so and then never added the leaves
        # themselves. Qualified `baseline_build.enabled` / `baseline_build.timeout_s` occur
        # nowhere for real; both decided via qualified match.
        "fleet.yaml:preflight.baseline_build",             # settings.py:287
        "fleet.yaml:preflight.baseline_build.enabled",     # settings.py:274
        "fleet.yaml:preflight.baseline_build.timeout_s",   # settings.py:275
        #
        # --- timeouts and ceilings with no consumer ---------------------------------------
        # `budgets.build_timeout_s` left this list at ADR-0080 (§11.5 step 8): `cli.resume`
        # resolves the continuation's `_build_impl` timeout from it rather than from a literal,
        # so it has a real reader and `test_known_inert_keys_are_still_inert` fails while the
        # line is here. D50's group-4 body carries the dated marker.
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
        # `LlmSection.cache_path`. STILL INERT after the 2026-08-25 cache wiring, and separately
        # judged from `cache_mode` rather than swept along with it: `RunContext.__post_init__`
        # builds `SqliteLlmCacheStore(writer=..., read_conn=...)`, so the store rides the §11.5
        # single writer and the §6 database — it takes no path at all, from this key or anywhere.
        # `cache_path` occurs nowhere in `src/fleet/` outside `settings.py`.
        "fleet.yaml:llm.cache_path",                       # `LlmSection.cache_path`
        #
        # --- revealed by comment/docstring stripping (was passing on prose alone) --------
        # `orchestrator/budgets.py:975`'s docstring names it; `Limits.create`'s `llm_overrides=`
        # kwarg is never passed by any of its five call sites (`cli.py:1812,4066,7488,7560,9107`).
        # `settings.py:1253`'s `llm_concurrency` accessor DOES apply it correctly — but nothing
        # outside `settings.py` calls `llm_concurrency` either (only `tests/test_settings.py`).
        "fleet.yaml:llm.concurrency_overrides",             # settings.py:683
        # `validate_memory_budget` (settings.py:1272) IS now called — round VI task 24
        # (`c73d023`/`84800a7`) wired `cli.py::_load_settings` to call
        # `settings.validate_memory_budget(host_total_mb=_read_host_mem_total_mb())` after every
        # `FleetSettings.load()`, so the value now genuinely affects behavior (exit 2 on breach).
        # At that point it was left annotated rather than removed: the field was still read only
        # *indirectly* through `validate_memory_budget`'s own internal `self.max_host_rss_mb`
        # access, and the literal token did not yet appear at any cli.py call site, so the plain
        # bare-name scan this file uses could not see that form of wiring.
        # `fleet.yaml:budgets.max_host_rss_mb` REMOVED 2026-09-03 (round VI task 36): task 27/29's
        # `_host_memory_sampler` (`cli.py:2010`) and the two `_run_*_wave` composition roots
        # (`cli.py:8619`, `:8710`) construct `HostMemorySampler(..., max_host_rss_mb=
        # settings.config.budgets.max_host_rss_mb, ...)` / `max_host_rss_mb=config.budgets.
        # max_host_rss_mb` — a direct, literal attribute read outside settings.py, distinct from
        # and in addition to task 24's indirect `validate_memory_budget` call. The bare-name scan
        # now finds it for real (`_readers("max_host_rss_mb")` returns `['cli.py',
        # 'orchestrator/memory_guard.py']`), so `test_known_inert_keys_are_still_inert` started
        # failing — correctly: the key is no longer inert by this file's own predicate, and
        # `test_every_config_key_is_read` agrees (removing the line changes nothing there, because
        # `_inert_keys()` already excludes this key once real readers exist). Per this file's own
        # ratchet, "a key leaving this list only ever means 'now proven read'" — verified by
        # running the full file after the removal (see task-36-report.md for the run). See D50
        # in docs/INTEGRATION_HONESTY.md for the annotated ledger history of this key.
        # `fleet.yaml:pr.merge_wait_timeout_s` REMOVED 2026-08-30 (round M, D80 wiring,
        # `9c20eeb`): `cli.py`'s `_resume_impl` now passes `open_pr_max_age_s=
        # float(settings.config.pr.merge_wait_timeout_s)` into `orchestrator/stubs.py::reconcile`,
        # a real deadline check against a real held-PR row — closing the exact deferred item
        # `docs/PROGRESS.md:5751` named ("the wiring lane must delete the KNOWN_INERT line...
        # when it first passes the real setting"). Per this file's own ratchet: a key leaving this
        # list only ever means "now proven read", never "found unnecessary" — removal is a stricter
        # claim than presence, so it stays deleted rather than commented out.
        # `obs/redact.py:72-73` hard-codes `ENTROPY_MIN_BITS = 4.0` / `ENTROPY_MIN_LEN = 20` —
        # the same defaults as the config, but as module constants, not reads. The `#:` comment
        # above them just cross-references the config keys it does not consult.
        "fleet.yaml:redaction.entropy_min_bits",            # settings.py:307
        "fleet.yaml:redaction.entropy_min_len",             # settings.py:308
        #
        # --- revealed by blanking string literals generally, not just docstrings ----------
        # (`fleet.yaml:run.stale_after_s` used to sit here. It left when `fleet resume` §11.5
        # step 3 began deriving its staleness cutoff from it. The HALF of that entry which is
        # still true — nothing constructs `phases.heartbeat_ttl_seconds` from the key, so
        # `schema.sql`'s hardcoded 300 is what a claimed phase carries — is recorded in
        # docs/INTEGRATION_HONESTY.md rather than lost with the line.)
        # `workers/symbolindex.py:107`'s `MARKER_SCAN_BYTES: Final = 4096` is the same defect
        # class, same section as `entropy_min_bits`/`entropy_min_len` above, one file over: a
        # hardcoded module constant with the config key's default baked in, plus a bare
        # `"""...marker_scan_bytes..."""` string right after it (not a docstring — it is not the
        # first statement of the module — so it survived the original strip too) that named the
        # key in prose and was the only thing making the old scan pass.
        "fleet.yaml:scan.contracts.marker_scan_bytes",      # settings.py:337
        #
        # --- generic-word / same-name-different-object collisions, decided via qualified match --
        # `transform.ladder[i].role`: `.tier` is its sibling and stays genuinely undecidable (see
        # `UNVERIFIABLE`); `.role` IS decidable — `ladder.role` occurs nowhere for real once
        # qualified.
        "fleet.yaml:transform.ladder.role",                 # settings.py:419
        # (`transform.ladder.context_policy` used to sit here, with the same "occurs nowhere for
        # real once qualified" mechanism — round EE (2026-09-02) wired it for real:
        # `orchestrator/runner.py:523`'s `_drive` now sources `ladder=tuple(rung.context_policy
        # for rung in self.ctx.config.transform.ladder)`, closing §12.35's CLI-level proof. It
        # keeps its `QUALIFIED_MATCH_KEYS` membership below. The qualified scan's own
        # syntactically-derived parent name (`ladder`, from the dotted config path) does NOT match
        # the genuine reader's actual variable name (`rung`) — round EE's own final review found
        # this gap, which would have let the wiring land invisibly to this ratchet. Closed, not
        # just disclosed: `_PARENT_FIELD_OVERRIDES` (below `_parent_field`'s definition) maps this
        # one key to `rung` explicitly, so the qualified scan finds the real reader.)
        # (`llm.max_schema_repairs`, `llm.failover.enabled` and `llm.failover.max_targets_per_call`
        # used to sit here, with the mechanism spelled out: `RunContext.llm_policy` was declared
        # and consumed but never assigned, so `CallPolicy()` was always all-defaults and the bare
        # names passed the plain scan on `self._policy.max_schema_repairs` /
        # `.max_targets_per_call` — real code, wrong object. They left on 2026-08-22 when
        # `RunContext.__post_init__` began deriving the policy from `config.llm` through
        # `orchestrator/context.py::call_policy_for`. They keep their `QUALIFIED_MATCH_KEYS`
        # membership: the wrong-object collision is unchanged, and it is the only thing that
        # would hide a revert. Their three still-inert siblings are above, under §11.8.)
        # `cli.py` declares a `--stub-blocked` flag / local named `stub_blocked` at five
        # sites (2377, 3046, 3120, 4770, 9536) — never `config.transform.stub_blocked`. The
        # refusal block `cli.py:3156-3158`/`4772-4774` even says so: "--stub-blocked is not
        # implemented". Qualified `transform.stub_blocked` occurs nowhere for real.
        "fleet.yaml:transform.stub_blocked",                # settings.py:453
        # (`llm.cache_mode` used to sit here, and the mechanism was: `GlobalOptions.cache_mode`
        # was derived entirely from `--llm-cache` and never from `config.llm.cache_mode`, so
        # every reader of the bare name read that property rather than settings, and
        # `RunContext.llm_cache_mode` was never constructed from config either. It left on
        # 2026-08-25, when `cli._load_settings` began routing `--llm-cache` through
        # `overrides["llm.cache_mode"]` and `RunContext.__post_init__` began resolving the mode
        # from `self.config.llm.cache_mode`. It keeps its `QUALIFIED_MATCH_KEYS` membership: the
        # bare name still collides with `cli.GlobalOptions.cache_mode`, which still exists and
        # is still the flag-only view, so the plain scan remains useless for this key and
        # dropping the membership is what would hide a revert of the wiring. Same shape and same
        # remedy as the `llm.max_schema_repairs` note above.
        #
        # What this ratchet does NOT catch, measured rather than assumed: reverting the
        # CONSUMPTION point alone — `RunContext.__post_init__` back to not installing a cache,
        # with `cli._load_settings`'s `overrides["llm.cache_mode"]` left in place — leaves this
        # whole file GREEN, because that dotted key is a plain string literal and `_sources()`
        # blanks comments and docstrings but not string literals generally, so the ROUTING half
        # satisfies the qualified scan by itself. Reverting BOTH halves does fail here, in two
        # tests. The ratchet against the consumption point is therefore not a text scan at all:
        # it is `tests/test_run_context_llm_cache.py`, which asserts a backend call log and an
        # `llm_cache` row count. Recorded here so the next reader does not mistake this line's
        # absence from KNOWN_INERT for a guarantee it cannot give.)
    }
)

QUALIFIED_MATCH_KEYS: frozenset[str] = frozenset(
    {
        # These leaves are read by the plain bare-name scan (`_readers`) for reasons that have
        # nothing to do with the config: a common English word, or a same-named field/flag/local
        # on an unrelated object. `_inert_keys()` decides them with `_qualified_readers` instead —
        # the immediate parent field name plus the leaf (`failover.enabled`,
        # `llm.max_schema_repairs`). Membership says only *which scan decides the key*, never
        # what the verdict is: for most of these the qualified pair occurs nowhere and the
        # verdict is inert, while the three `llm.` ones are genuinely READ through it since
        # 2026-08-22 (`orchestrator/context.py::call_policy_for`), and `transform.ladder.
        # context_policy` is genuinely READ since 2026-09-02 (round EE,
        # `orchestrator/runner.py:523`) — all four are no longer in `KNOWN_INERT`. They stay here
        # because the bare-name collision that made the plain scan useless for them is unchanged,
        # and it is what would let a revert of that wiring go unnoticed. `transform.ladder.
        # context_policy`'s own qualified pair (`ladder.context_policy`) still would not catch a
        # revert either, since the real read is spelled `rung.context_policy` — see the
        # `KNOWN_INERT` comment above for the full disclosure. See the `KNOWN_INERT` comments
        # above for each specific collision.
        "fleet.yaml:transform.ladder.role",
        "fleet.yaml:transform.ladder.context_policy",
        "fleet.yaml:llm.max_schema_repairs",
        "fleet.yaml:llm.failover.enabled",
        "fleet.yaml:llm.failover.max_targets_per_call",
        "fleet.yaml:llm.rate_limit.aimd.floor",
        "fleet.yaml:transform.anchoring.enabled",
        "fleet.yaml:preflight.baseline_build.enabled",
        "fleet.yaml:preflight.baseline_build.timeout_s",
        "fleet.yaml:transform.stub_blocked",
        "fleet.yaml:llm.cache_mode",
    }
)

UNVERIFIABLE: frozenset[str] = frozenset(
    {
        # Not prose (stripped) and not a generic word resolved by qualification: the text
        # `ladder.tier` genuinely occurs in real, non-comment code at `orchestrator/runner.py:802`
        # — `ladder.tier(attempt)` — but `ladder` there is `orchestrator/retry.py`'s `LadderState`
        # (`retry.py:83`), whose OWN `tier` is a method (`retry.py:100`). **Corrected 2026-09-02
        # (round EE): `LadderState.tier` used to be derived purely from the hardcoded
        # `workers/base.TIER_LADDER`, and `LadderState(...)` at `runner.py:512` used to take no
        # `ladder=` kwarg — both now false.** `.tier()` now delegates to the parameterized
        # `workers.base.tier_for_attempt(attempt, self.ladder)`, and `runner.py:512` DOES pass
        # `ladder=` — the configured ladder genuinely reaches `.tier()`. The verdict is unchanged
        # regardless: `.tier()`'s value is *derived* from `.context_policy` (via
        # `workers.base._TIER_FOR_RUNG`), never read from `config.transform.ladder[i].tier` — that
        # declared field is still never read by anything, for the same structural reason as
        # before, just not the reason originally written down here. "Attribute of a Pydantic
        # model" vs. "method of an unrelated same-named object" is still not a distinction a text
        # scan, qualified or bare, can draw. Its siblings `.role` and `.context_policy` ARE
        # decided (see `QUALIFIED_MATCH_KEYS`) — this is the genuine remainder.
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
        # `orchestrator/budgets.py:980` calls to size the per-tier semaphore. Sibling `heavy`
        # still passes the bare scan on an unrelated match of that word, so it needs no entry
        # here — `workhorse` and `cheap` below do not, and are read exactly the same way.
        "fleet.yaml:concurrency.llm.workhorse",            # settings.py:224
        # Same accessor, same call site, the `self.cheap` fallback branch of the `{...}.get(tier,
        # self.cheap)` in `for_tier` (settings.py:229). Used to pass the bare scan only on
        # `models/state.py:101`'s unrelated `Field(description="...cached for cheap
        # comparison")` — a string literal, not code, blanked once the stripper stopped
        # special-casing docstrings. Genuinely read the same way `workhorse` is.
        "fleet.yaml:concurrency.llm.cheap",                # settings.py:225
        # Read by the §9 rule-3 startup check (settings.py:1410-1420): a tier whose first target
        # declares a smaller `max_context` is a `ConfigValidationError` at load. Enforced in
        # full; the enforcement simply lives in the settings module.
        "fleet.yaml:llm.require_capabilities",             # settings.py:684
        "fleet.yaml:llm.require_capabilities.min_context",  # settings.py:671
        # `FleetSettings.load` (settings.py:1147) passes `config.redaction.patterns` to
        # `_refuse_secret_material`, which is called for every §9 source file and refuses one
        # that matches a configured pattern (settings.py:1299-1310); `RedactionSection`'s own
        # `model_validator` (settings.py:314) also compiles every pattern to catch a bad regex
        # at load. An independent sweep called this key inert on the strength of an unrelated
        # `patterns:` kwarg collision in `workers/symbolindex.py:573` — that collision is real,
        # but it is not the only reason the bare scan passes; the key is genuinely enforced too.
        "fleet.yaml:redaction.patterns",                   # settings.py:294
        # `_check_redaction_switch` (settings.py:1314-1320), called from `FleetSettings.load`
        # (settings.py:1149), refuses to load if `redaction.enabled` is false and
        # `FLEET_ALLOW_RAW` is not `"1"`. Same independent-sweep miscall as `patterns` above —
        # the bare scan's pass is not solely the `scan.contracts.enabled` collision it named.
        "fleet.yaml:redaction.enabled",                    # settings.py:293
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
    """Blank every `#` comment and every string literal, so a config key name that appears only in
    prose can no longer be mistaken for a read.

    `tokenize` finds comments (a `COMMENT` token, unambiguous). `ast` finds string literals:
    originally this only blanked module/class/function *docstrings* (a string literal is a
    docstring by *position* — the first statement of such a body — which a regex cannot decide),
    but that left every OTHER string literal scannable — concretely, `models/state.py`'s
    `Field(description="...")` kwarg for `heartbeat_ttl_seconds` names `stale_after_s` in prose and
    was passing the scan on that alone. Blanking every `ast.Constant` holding a `str`, regardless
    of position, closes that hole without a regex (a regex cannot tell a string literal from
    surrounding code without parsing).

    This also blanks a string a key is genuinely read *through* — `getattr(cfg, "key")`, a dict
    lookup by string key — which would wrongly mark such a key inert. No config-model field this
    test walks is read that way anywhere in `src/fleet/` today (checked by hand: every
    `getattr(...)`/`.get(...)` call with a string literal in the tree targets something other than
    a settings-model attribute), so the blanket blank is safe in practice, not just in theory.
    """
    lines = source.splitlines(keepends=True)

    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            _blank_span(lines, tok.start, tok.end)

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.end_lineno is not None and node.end_col_offset is not None
            _blank_span(
                lines,
                (node.lineno, node.col_offset),
                (node.end_lineno, node.end_col_offset),
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
    same-named field, so a hit on the bare name proves nothing either way."""
    pattern = re.compile(rf"\b{re.escape(parent)}\s*\.\s*{re.escape(field_name)}\b")
    return [
        str(path.relative_to(SRC)) for path, text in _sources() if pattern.search(text)
    ]


#: Round EE (2026-09-02): `transform.ladder.context_policy`'s genuine reader
#: (`orchestrator/runner.py:523`) spells the qualifying variable `rung`, not `ladder` — a real,
#: intentional Python identifier choice (one element of the ladder, not the ladder itself), not a
#: naming accident. `_parent_field`'s syntactic derivation from the DOTTED CONFIG PATH cannot see
#: this: `transform.ladder.context_policy`'s path segment is `ladder` regardless of what the
#: reading code calls its own local variable. Overriding here — rather than renaming the config
#: path (which would be a real, disruptive `fleet.yaml` schema change for a test-scan
#: convenience) or leaving this key structurally unverifiable forever — is what gives this key's
#: `QUALIFIED_MATCH_KEYS` membership real teeth again, matching every other member's actual
#: contract: "a non-empty reader list, not a hand-maintained claim."
_PARENT_FIELD_OVERRIDES: dict[str, str] = {
    "fleet.yaml:transform.ladder.context_policy": "rung",
}


def _parent_field(qualified: str) -> str:
    """`fleet.yaml:llm.failover.enabled` -> `failover` — the immediate parent field name.

    Consults `_PARENT_FIELD_OVERRIDES` first for the rare key whose genuine reader qualifies
    against a Python identifier that differs from the config path's own segment name.
    """
    if qualified in _PARENT_FIELD_OVERRIDES:
        return _PARENT_FIELD_OVERRIDES[qualified]
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

    The exact count (182, at time of writing) is a tripwire on its own: `len(keys) > 100` would
    still pass if an entire section vanished from the walk (`verify` alone is 8 keys), so the
    per-section coverage loop below is the one that actually catches that regression — the exact
    count just makes any drift, section-sized or not, visible instead of silently tolerated.
    """
    keys = _config_keys()
    assert len(keys) == 182, (
        f"walked {len(keys)} keys, expected 182 — recount deliberately (a key was added/removed, "
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


def test_qualified_match_keys_resolve_to_a_verdict() -> None:
    """`QUALIFIED_MATCH_KEYS` is an implementation detail of `_inert_keys()`, not its own
    verdict — every member must resolve to a real classification or this set is silently
    steering keys nowhere.

    There are exactly two ways to resolve. Either the qualified pair occurs nowhere, the key
    is inert, and it is in `KNOWN_INERT`; or the qualified pair occurs for real, the key is
    read, and this test demands the **evidence** — a non-empty reader list — rather than
    membership in a hand-maintained set. The predecessor of this test asserted
    `QUALIFIED_MATCH_KEYS <= KNOWN_INERT`, which was true while no member had ever been
    wired; it would have forced the first member that *was* wired out of
    `QUALIFIED_MATCH_KEYS` and back onto the bare scan — the scan that is useless for these
    keys by construction — silently retiring the ratchet for exactly the key that had just
    stopped being a defect.
    """
    for key in sorted(QUALIFIED_MATCH_KEYS):
        name = _config_keys()[key]
        readers = _qualified_readers(_parent_field(key), name)
        if key in KNOWN_INERT:
            assert not readers, (
                f"{key} is in KNOWN_INERT but its qualified pair is read by {readers} — "
                "`test_known_inert_keys_are_still_inert` should already have caught this"
            )
            continue
        assert readers, (
            f"{key} is not in KNOWN_INERT, so it is claimed to be READ — but the qualified "
            f"`{_parent_field(key)}.{name}` pair occurs nowhere in src/fleet/. It resolves to "
            "no verdict at all: put it back in KNOWN_INERT, or drop it from "
            "QUALIFIED_MATCH_KEYS if the bare-name collision is gone."
        )
