# Plan: Security Review Remediation Round

**Spec:** This plan's authority is `SECURITY_REVIEW.md` (repo root, untracked, 899 lines,
last touched 2026-09-15) plus `docs/SPEC.md` where a task corrects a SPEC/CRITERIA_PLAN claim.
`SECURITY_REVIEW.md` is explicitly NOT a SPEC/ADR/D-number artifact (its own header says so) —
it is the finding source; this plan is what turns its findings into code.

**Severity reconciliation (a ruling, logged here per CLAUDE.md Rule 1):** the dispatching
request asked for "10 Important and 4 Minor findings." `SECURITY_REVIEW.md` does not use that
split — it labels findings CRITICAL, Important, or Minor in prose, non-uniformly. This plan's
14 tasks are 5 CRITICAL + 5 Important (= the requested "10 Important", read as "10 non-Minor")
+ 4 Minor, derived directly from the document's own severity words per finding, not relabeled
to force the count. If this reconciliation is wrong, it costs a re-triage of which tasks are
"Important" vs "Critical" — the fix content itself does not change.

## Global Constraints

- Work happens in worktree `.claude/worktrees/security-review-fixes` (already provisioned:
  `tools/bin/*`, `.venv` present). Never `cd` out of it.
- Every task: run its own covering tests (`pytest <specific file(s)>`, not the full suite) and
  report the command + output in the report file. The full suite runs once, at the final review
  gate, per CLAUDE.md §6.
- **Never export `FLEET_*`** in any shell used for tests (see CLAUDE.md §6) — `extra="forbid"`
  on `FleetConfig` makes one stray var fail every settings load.
- Redaction tasks (T6, T7): `src/fleet/obs/redact.py` exposes `redact(value: JSONValue) ->
  JSONValue` (handles `str` or `Mapping[str, JSONValue]` via `@overload`) and
  `redact_mapping(payload: Mapping[str, JSONValue]) -> dict[str, JSONValue]`. Use whichever
  matches the call site's existing type; do not write a new redaction helper.
- `classify_build_failure()` in `src/fleet/workers/buildverify.py` (~line 412) has a load-bearing
  docstring constraint: **"from mechanical evidence only — never from the message."** No task in
  this plan may add message/stderr-text sniffing to that function or its callers. This is why
  the Cargo-network finding (T5) is scoped as a documentation task, not a classifier change —
  do not "fix" it by parsing `cargo fetch`'s stderr for a hostname-resolution string.
- Citations below were fact-checked against this worktree's current source
  (`b58f43f` base) immediately before this plan was written — treat a citation that no longer
  matches as the tree having moved since, not as this plan being wrong; re-locate by symbol/grep
  and report the drift rather than silently editing the wrong site.
- Do not touch `.claude/settings.json`, `.claude/settings.json.graphify-bak`, `graphify-out/`,
  or `references/migrating-a-repository-to-bazel.md` — pre-existing uncommitted state in the
  primary checkout, unrelated to this plan, and outside this worktree's branch history anyway.

---

## Task 1 [CRITICAL] — refuse to dereference a symlink in the rewrite worker's file read

**Finding:** `SECURITY_REVIEW.md` item #7 (lines 14-90). A source repo can commit a tracked
symlink to an arbitrary host path. `src/fleet/cli.py::_tracked_at()` (~line 6233) lists it with
no type filter; `src/fleet/workers/rewrite.py:452`,
`source = (root / unit).read_text(encoding="utf-8")`, follows it like any other file and the
content flows into `_evidence()`'s `"current_content"` (line ~804) — an LLM-bound field, per
item #4. `_targets_are_present()` (~line 1010) uses `.is_file()`, which also follows symlinks
and gives no protection.

**Fix:** in `rewrite.py`, immediately before the `read_text()` call at line 452, check
`(root / unit).is_symlink()` (this does NOT follow the link) and, if true, refuse with a named,
loud failure — do not silently skip or fall through to a different content source. Use this
worker's existing error-class convention (grep other `raise` sites in `rewrite.py` for the
pattern already in use, e.g. `ConfigFileError`/a repair-ladder-visible exception) rather than a
bare `Exception`. Also audit `_targets_are_present()` (~line 1010): it should not report `True`
(present) for a symlinked path either, since that path exists only to gate whether a rewrite is
attempted at all.

**Test:** a new test exercising `rewrite.py`'s read path against a worktree fixture containing a
real OS-level symlink (pointing at a file outside the fixture's own tree) — assert the read is
refused with the new failure, not that it silently returns the target's content. Cover
`_targets_are_present()` too: assert it does not report the symlinked path as present.

---

## Task 2 [CRITICAL] — constrain `BuildTarget.rule` / `ProposedBuildTarget.rule` against Starlark injection

**Finding:** `SECURITY_REVIEW.md` item #6 (lines 93-214). `src/fleet/bazel/generators.py`'s
`render_target()` (~line 103) does `lines = [f"{target.rule}("]` — the LLM-controlled `rule`
string is written verbatim, unescaped, as the head of a generated Starlark function call in a
real `BUILD.bazel` that `bazel build` evaluates. `src/fleet/models/build.py:73`
(`BuildTarget.rule: str = Field(min_length=1, ...)`) and `src/fleet/llm/schemas.py:244`
(`ProposedBuildTarget.rule: str = Field(min_length=1, max_length=100)`) are both free text with
no `Literal`/pattern, unlike the sibling `VersionConflictResolution.mechanism`
(`llm/schemas.py:281-285`, `pattern=r"^(bazel_dep|single_version_override)$"`), which is the
precedent to follow.

**Fix:** add a `pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$"` constraint to both `BuildTarget.rule`
(`src/fleet/models/build.py:73`) and `ProposedBuildTarget.rule` (`src/fleet/llm/schemas.py:244`)
— reject at the LLM-schema boundary (the earlier, cheaper place) as well as the internal model,
per the finding's own remediation note. Every real Bazel rule name (`java_library`, `ts_project`,
`go_test`, `filegroup`, etc.) already satisfies this pattern, so no legitimate rule is excluded.

**Test:** a Pydantic validation test asserting `BuildTarget(rule="filegroup(name = \"x\")\n#", ...)`
and `ProposedBuildTarget(rule=...)` with the injection string from the finding's write-up both
raise `ValidationError`, and a normal rule name (`"java_library"`) still validates.

---

## Task 3 [Minor] — escape `render_target()`'s attribute names and `_split_extension()`'s var/tag

**Finding:** `SECURITY_REVIEW.md` item #6's "Minor, related finding" (lines 157-160) and the
"Two latent siblings" block (lines 197-207). `render_target()`'s attribute *names* from
`target.attrs` (`generators.py:109-110`) and `render_module_bazel()`'s `var`/`tag` from
`_split_extension()` (`generators.py:700-707, 903, 933`) are rendered as bare, unescaped
identifiers — same shape as Task 2's bug, but confirmed **not currently LLM-reachable** (no
schema field populates `target.attrs`; the `var`/`tag` values come only from deterministic
ecosystem-adapter code today). Defensive fix, not an active exploit.

**Fix:** validate these identifiers the same way as Task 2 — either a shared regex constant
(`^[a-zA-Z_][a-zA-Z0-9_]*$`) both Task 2 and this task import, or a loud rejection at render
time if an attr name / var / tag doesn't match it. Coordinate with Task 2's implementer output if
it already introduced a reusable pattern constant (check `git log`/`generators.py` before
duplicating it — this task is likely dispatched after Task 2 lands).

**Test:** a rendering test with a deliberately malformed attr name / var / tag asserts a loud
rejection rather than silent pass-through into generated text.

---

## Task 4 [Important] — `VerifySection.network` must not be a plain, env-overridable string

**Finding:** `SECURITY_REVIEW.md` item #5, Finding A (lines 217-350, "CONFIRMED" at line 289).
`src/fleet/settings.py:739`: `network: str = "none"`, inside a `pydantic-settings` model with
`env_prefix="FLEET_"` and `env_nested_delimiter="__"` — so `FLEET_VERIFY__NETWORK=bridge` (or
`host`) silently overrides the sandbox's `--network=none` hermeticity guarantee with zero
validation error. `container.py`'s own docstring calls that guarantee "impossible by
construction"; it currently is not.

**Fix:** change `src/fleet/settings.py:739` from `network: str = "none"` to
`network: Literal["none"] = "none"`. Check `Literal` is already imported in that file (it is
used elsewhere in the models per the finding's own citations of `Literal["COORDINATE", ...]`
patterns elsewhere in this codebase — verify in `settings.py` specifically). Do **not** touch
the separate, deliberately-networked `container_network: str = "bridge"` field (line ~253, the
"native baseline" comparison path) — that one is explicitly out of scope per the finding.

**Test:** a settings test asserting `FleetConfig` construction with
`FLEET_VERIFY__NETWORK=bridge` in the environment now raises a `ValidationError` (or the
project's equivalent settings-load error), where the existing
`tests/test_settings.py:199`-area test only ever asserted the default. Do not remove the
existing default-value assertion; add the rejection case beside it.

---

## Task 5 [Important] — disclose the Cargo / `--network=none` gap; no classifier change

**Finding:** `SECURITY_REVIEW.md` item #5, Finding B (lines 258-349). Empirically confirmed (a
real `--network=none` Docker run against a throwaway Rust crate, table at lines 304-308): Cargo's
`cargo fetch` needs network access under sandboxed verification, lockfile or not — affecting
~33 of 269 scanned Gitea repos (~12%). **This is a documentation task, not a code-classifier
task** — see Global Constraints' note on `classify_build_failure()`'s mechanical-evidence-only
docstring, which forbids the "add a network-shaped `FailureClass`" remediation the finding
tentatively offered, because there is no mechanical (non-message-parsing) signal that
distinguishes this Cargo-registry DNS failure from any other Bazel build error at the exit-code
level available to that function.

**Fix:**
1. Add an ADR to `docs/DECISIONS.md` (next free ADR number — read the file's tail to find it;
   do not guess a number, and do not renumber any existing ADR) stating: Cargo repos require
   network access during sandboxed (`--network=none`) verification, confirmed empirically
   2026-09-15; this is a known, disclosed limitation of the current `crate_universe` integration,
   not silently masked; the currently-affected repos surface as ordinary `BUILD_ERROR` retries
   that exhaust the repair ladder without a distinguishing signal, because
   `classify_build_failure()` deliberately classifies from mechanical evidence only; a structural
   fix (a Cargo-specific offline vendor cache, analogous to `ecosystems/go.py`'s zero-network
   Gazelle approach) is the documented follow-up, not attempted in this round.
2. Update `SECURITY_REVIEW.md` item #5's Status line (currently "OPEN, Finding A and B both
   confirmed") to note Finding A is fixed (Task 4) and Finding B is disclosed-not-fixed, citing
   the new ADR number.

**Test:** none (documentation-only) — the implementer's report should instead confirm the ADR
number chosen doesn't collide with one already in the file (grep `docs/DECISIONS.md` for
`### ADR-<n>` or this project's actual heading form before picking the number).

---

## Task 6 [CRITICAL] — wire redaction into the core LLM egress path

**Finding:** `SECURITY_REVIEW.md` item #4 (lines 353-550), core gap (Question 1, lines 432-436).
`src/fleet/llm/calls.py::render_prompt()` (~line 257) JSON-serializes the caller's `evidence`
mapping directly into the outbound `Message` with no redaction call anywhere in `llm/client.py`
or `llm/calls.py`. Concrete exposure: `rewrite.py::_evidence()`'s `"current_content": source`
(line ~804) — a repo file's raw content, potentially containing a hardcoded credential — reaches
whichever third-party model backend is configured, unredacted.

**Fix:** in `src/fleet/llm/calls.py::render_prompt()` (~line 257), apply `redact()` (or
`redact_mapping()` — see Global Constraints for which signature fits) to `evidence` before it is
JSON-serialized into the outbound `Message`. Read the function's docstring first — it states the
render must be "byte-identical for identical inputs" because its output feeds `prompt_sha256()`
(the cache key, ~line 279); redacting before serialization is consistent with that contract
(same input → same redacted output → same hash), so redact first, then `json.dumps` the redacted
mapping, not the original. Do not change `prompt_sha256()` itself — it already hashes whatever
`render_prompt()` returns.

**Test:** a test calling `render_prompt()` with an `evidence` dict containing a value
`redact()`/`redact_mapping()` is known to transform (check `tests/test_redact.py` or
`obs/redact.py`'s own tests for a fixture secret shape already used elsewhere in this repo, for
consistency) and asserting the rendered `Message.content` does not contain the raw secret. Also
run the existing `llm/calls.py`/`llm/client.py` test suite to confirm `prompt_sha256` callers and
cache-hit behavior are unaffected by the added redaction step (a cache-key test already exists
per item #4 Question 2's finding that `llm/cache.py` is redaction-safe — do not duplicate it,
just confirm it still passes).

---

## Task 7 [CRITICAL] — wire redaction into the PR body and PR title before they are posted

**Finding:** `SECURITY_REVIEW.md` item #4's "NEW finding — Important/Critical (public-facing)"
(lines 438-457) and the "ESCALATION" section's "Two more unredacted call sites" (lines 521-525).
`src/fleet/workers/prwriter.py`: `write_pr_title`'s `title = proposed.value.title` (line ~442,
returned as `title[:120]`) and `render_body()`'s `notes = prose.value.body` (line ~434, folded
into the returned body string) both carry raw LLM-generated prose to a real, public
GitHub/Gitea PR with zero redaction calls anywhere in the file (`prwriter.py` has no
`from fleet.obs.redact import` at all, per the finding). `render_body()` is also called a second
time on every PR-promotion/revalidation round via `cli.py::_regenerate_pr_body()` (~lines
15877-15933) — the same fix must cover that call path too (it calls the same function, so fixing
`render_body()` itself covers it; do not patch `_regenerate_pr_body()` separately unless it
builds body text some other way — verify by reading it).

**Fix:** in `src/fleet/workers/prwriter.py`, apply `redact()`/`redact_text()` (whichever fits —
these are `str` values, so likely `redact_text()`; check its signature in `obs/redact.py`) to
`title` in `write_pr_title` before the `title[:120]` truncation (or after — decide based on
whether redaction could change length meaningfully; document the choice in the report), and to
the assembled body string in `render_body()` before it is returned — cover `notes` specifically
since that's the traced LLM-prose injection point, but redacting the whole assembled body (not
just `notes`) is the more robust match for `obs/redact.py`'s "EVERY egress boundary" design goal
the finding cites — prefer redacting the full returned string.

**Test:** a test constructing a `render_body()`/`write_pr_title()` call with LLM-prose input
containing a value `redact()` transforms, asserting the returned body/title string does not
contain the raw value. Also confirm `tests/test_pr_body_redaction.py` (the existing test the
finding says proves a narrower thing than SPEC.md claims — see Task 8) still passes unchanged;
its scope (the DB-mirror redaction at `cli.py::_write_pr_record`) is a different, already-correct
code path that this task does not touch.

---

## Task 8 [CRITICAL] — correct the SPEC.md and CRITERIA_PLAN.md claims that this was already fixed

**Depends on:** Task 6 and Task 7 landing first — this task corrects the documents to describe
what is now actually true, per CLAUDE.md's "Documents Are Inputs to Future Edits" guardrail
("'The SPEC says X but the code cannot do X' is two edits, not one") and Rule 14 (§12 criterion
wording changes need disclosed adjudication).

**Finding:** `SECURITY_REVIEW.md` item #4's "ESCALATION" (lines 500-538). `docs/SPEC.md:7175`
(§11.4's redaction-boundary list) states `workers/prwriter.py` "redacts the assembled PR body
**and** re-scans it after the LLM prose" — false before Task 7, per the finding's direct `grep`
(one hit, a comment, not a call). `docs/CRITERIA_PLAN.md:1468` marks §12.20 "Criterion DONE"
citing `tests/test_pr_body_redaction.py`, whose own docstring the finding quotes as proving only
the DB-mirror redaction, not the posted body.

**Fix:**
1. Re-read `docs/SPEC.md` around line 7175 and `docs/CRITERIA_PLAN.md` around line 1468 in
   **this** worktree at dispatch time (line numbers drift between commits per this project's own
   CLAUDE.md Guardrail 7 warning — locate by the quoted text, not blindly by line number).
2. Correct SPEC.md's §11.4 claim to accurately describe the post-Task-7 state: `prwriter.py`'s
   `render_body()`/`write_pr_title()` now redact before returning (cite the actual mechanism, not
   the old false "re-scans it after" phrasing).
3. Re-verify §12.20's actual DONE-bar against the corrected code (does redacting the posted body,
   not just the DB mirror, satisfy whatever §12.20's criterion text literally requires? Read the
   criterion text itself in SPEC.md's §12, not just CRITERIA_PLAN's summary line, before
   confirming DONE). If the criterion's own wording needs to change to reflect what's actually
   being asserted now, that is a Rule 14 event — add the dated in-place marker CLAUDE.md
   describes and either write an ADR or an explicit "adjudication pending" flag; do not silently
   reword the criterion.
4. Add a `docs/INTEGRATION_HONESTY.md` D-number entry for the divergence itself (SPEC/
   CRITERIA_PLAN asserted a redaction boundary that did not exist in code) — this is exactly the
   kind of "criterion text vs. what was actually built" gap that document exists to record, per
   the finding's own closing note. Use the next free D-number: derive it form-agnostically
   (union of the three heading forms CLAUDE.md describes), never from a range read out of a
   document.

**Test:** none required beyond what Tasks 6/7 already added — this is a documentation-accuracy
task. The implementer's report must show the exact `grep`/quote used to confirm the corrected
SPEC.md text now matches the corrected code, per this project's "bind prose to code" discipline.

---

## Task 9 [Minor] — wrap the Anthropic backend's API key in `SecretRegistry` consistently

**Finding:** `SECURITY_REVIEW.md` item #4, Question 5 (lines 477-487). `SecretRegistry`
(`src/fleet/settings.py:963`) exists specifically so an API key can't be carried as a plain `str`
into a traceback/log/`model_dump()`. `src/fleet/llm/backends/anthropic.py::_api_key()`
(line ~258-266) reads the key via `os.environ.get(name, "")` directly into a plain `str`,
bypassing `SecretRegistry` — a defense-in-depth inconsistency (not a demonstrated leak; the
finding found no path where this specific value reaches a log/error string unredacted today).

**Fix:** change `_api_key()` (or its caller at `anthropic.py:180`) to resolve the key through
`SecretRegistry` the same way other backends do — grep `src/fleet/llm/backends/*.py` for how a
sibling backend (e.g. `openai_compatible.py`/`bedrock.py`/`vertex.py`) resolves its key, and
match that pattern exactly rather than inventing a new one. If `anthropic.py` is the *only*
backend not using `SecretRegistry`, that confirms the finding's framing; if another backend has
the same gap, note it in the report (out of scope to fix here unless trivial — flag it as a
Minor deferred item per the SDD skill's process rather than silently expanding this task).

**Test:** a test asserting the resolved key is a `SecretStr` (or whatever `SecretRegistry`'s
wrapper type is) rather than a plain `str`, consistent with an existing test for a sibling
backend if one exists (reuse its shape).

---

## Task 10 [Minor] — add a symlink guard to the two harness-config-loading walks

**Finding:** `SECURITY_REVIEW.md` item #4, Question 4b (lines 468-475). `walk_files()`
(repo-content scanning) and `clone.py` already reject symlinks. Two lower-trust,
harness-config-authored walks do not: `src/fleet/rewrite/rules.py:251`
(`root.rglob("*")` inside `load_rules`/similar, loading `RewriteRule` YAML files — the loop
starting `for rule_file in sorted(p for p in root.rglob("*") if p.suffix in {".yaml", ".yml"}):`)
and `src/fleet/settings.py:1597` (`_check_rule_engines`, the identical `rglob("*")` pattern
loading rule-engine YAML). Confirmed lower severity than Task 1 — these walk the harness's own
config directories, not arbitrary repo content — but worth closing defensively since it's the
same unguarded pattern.

**Fix:** in both loops, skip (or refuse, matching Task 1's convention — pick refuse-loudly for
consistency, since a symlinked config file is not a legitimate configuration shape) any
`rule_file` where `rule_file.is_symlink()` is `True`, before it's opened/parsed.

**Test:** a test in each of the two areas (rules-loading, rule-engine-checking) with a symlinked
`.yaml` file in the scanned directory, asserting it's skipped/refused rather than loaded.

---

## Task 11 [Important] — fix semver pre-release misparse in both version-spec regexes

**Finding:** `SECURITY_REVIEW.md` item #1's "New finding" (lines 679-741) and item #3's
cross-reference. `src/fleet/bazel/generators.py:331`
(`_ATOM = re.compile(r"^(==|>=|<=|=|>|<|\^|~>|~)?\s*v?(\d+(?:\.\d+)*)")`) and
`src/fleet/graph/collisions.py:51` (`_VERSION_ATOM`, the same pattern plus `~=`) both use
`.match()`, which anchors only at the string's start — `1.2.3-beta.1` matches the numeric prefix
`1.2.3` and silently drops `-beta.1`, producing a **wrong** constraint (not merely a dropped
one) that MVS then trusts. Confirmed live via `_intersects(['==1.2.3-beta.1', '==1.2.3'])` →
`True` and `mvs_select(...)` on a `>=1.2.3-beta.1` spec → `'1.2.3'` (both reproduced in the
finding, lines 708-724). Real-world hits found in 2 of 6 sampled npm manifests
(`react-autocomplete@^1.0.0-rc2`, three hits in `activepieces`).

**Fix:** switch both `re.compile(...)` usages from `.match()` to `.fullmatch()` at their call
sites (not just the pattern definition — find where `_ATOM.match(...)` and
`_VERSION_ATOM.match(...)` are actually called, likely in `parse_range()`-shaped functions in
each file) OR anchor the pattern itself with a trailing `$` if `.match()` is kept — the finding's
own remediation note says `.fullmatch()` was verified by its investigating subagent to preserve
every currently-supported spec while converting a misparse into the already-tested
"unparseable → dropped" path (per item #1's original, deliberately-tested design,
`tests/test_bazel.py:438-443`). Do not change that existing "unparseable → dropped, empty
intersection raises" design — this task only fixes what counts as "unparseable."

**Test:** `_intersects(['==1.2.3-beta.1', '==1.2.3'])` must no longer be `True` under the old
wrong reasoning (assert it now correctly treats `1.2.3-beta.1` as unparseable-by-this-grammar,
per the existing dropped-spec path — or, if `parse_range()` is extended to actually understand
pre-release tags instead, assert it computes the *correct* intersection; pick whichever the
existing `parse_range()` grammar naturally supports, don't invent pre-release-aware version
comparison if it doesn't already exist). `mvs_select()` on the `>=1.2.3-beta.1` example must no
longer silently produce `'1.2.3'` from a misparsed spec. Confirm every spec shape
`tests/test_bazel.py`/`tests/test_collisions_wiring.py` currently expects to parse still parses
identically (run those files, not just the new test).

---

## Task 12 [Important] — detect a pre-existing BUILD.bazel/WORKSPACE/MODULE.bazel overwrite

**Finding:** `SECURITY_REVIEW.md` item #2 (lines 745-898). `src/fleet/workers/buildgen.py`'s
`_write()` (~line 672-679) unconditionally overwrites `<dest>/BUILD.bazel` (~line 390) and
`<dest>/MODULE.bazel` (~line 581) with generated content — no read of the pre-existing file, no
diff, no record. A source repo's own hand-written Bazel files (confirmed non-trivial in the
local Gitea corpus scan: `codex`/`codex-sovereign`, ~70 files each) are silently discarded in the
working tree (though preserved in git history via the ingest commit). `CollisionFinding.kind`
(`src/fleet/models/graph.py`, `Literal["COORDINATE", "CONTRACT", "DEST_PATH", "FILE_PATH",
"DEP_VERSION"]`, `repo_ids: list[RepoId] = Field(min_length=2)`) cannot represent this — every
kind requires ≥2 *different* repos; this is one repo's own file versus its own later-generated
replacement, structurally unrepresentable there. Do not add a new `CollisionFinding.kind` for
this — that model's `repo_ids` invariant doesn't fit and reshaping it is out of this round's
scope.

**Fix:** before `_write()` overwrites the destination `BUILD.bazel`/`WORKSPACE`/`MODULE.bazel`
path at the two call sites (~line 390, ~line 581), check whether the path already exists in the
worktree with content different from what's about to be written. If so, collect a short,
human-readable line (e.g. `"<dest-path>: replaced a pre-existing hand-written Bazel file"`) into
a list the caller can access — this becomes Task 13's input. Keep the collection mechanism
simple (a list attribute on whatever object already threads through `buildgen.py`'s per-repo
run, or a return value threaded up — match the existing code's own state-passing convention,
don't introduce a new global). Do not change `_write()`'s actual write behavior (the overwrite
itself is accepted, disclosed behavior per item #2's "RESOLVED" framing — the gap is the silence,
not the overwrite).

**Test:** a test running `buildgen.py`'s BUILD.bazel-generation step against a fixture repo that
already has a hand-written `BUILD.bazel` with different content, asserting the collected list
contains an entry naming that path. A second case (no pre-existing file, or identical content)
must produce an empty list — assert both.

---

## Task 13 [Important] — wire the overwrite disclosure into the PR's "Relocation map" section

**Depends on:** Task 12 (needs its collected-list mechanism).

**Finding:** `SECURITY_REVIEW.md` item #2's "follow-up questions RESOLVED" section (lines
855-861). `PrwriterInput.relocation_summary` (`src/fleet/workers/prwriter.py:140`) already
renders into a "### Relocation map" PR section (lines 573-574) but has **zero** real call sites
populating it anywhere in `src/fleet/` — confirmed via `git log -S relocation_summary` in the
finding. `docs/SPEC.md` (lines 876, 1539, 2424) requires this section to exist; the finding's own
conclusion is "the right fix is wiring the field, not deleting it."

**Fix:** find the real `PrwriterInput(...)` construction site(s) in `src/fleet/cli.py` (grep for
`PrwriterInput(` — the finding cites two candidate call sites around lines 15780 and 15902 in an
earlier version of this file; re-locate by symbol, not line number, since Tasks 6-12 may have
shifted line numbers by the time this dispatches) and pass Task 12's collected overwrite-list
into `relocation_summary=` at that call. This is a minimal, honest population of the field —
it does not need to cover every possible "relocation" the field's name might suggest (a fuller
relocation map covering ordinary file moves is a larger feature, out of this round's scope);
scope this task strictly to threading Task 12's overwrite-disclosure list through, since that is
the concrete, security-relevant gap this plan is closing.

**Test:** an end-to-end-ish test (or the narrowest test that exercises the real
`PrwriterInput(...)` construction site) asserting that when Task 12's detector finds an
overwritten file, the resulting PR body (via `render_body()`) contains the "### Relocation map"
section with that file named. Confirm the section is absent (not rendered) when there's nothing
to report — `render_body()`'s existing `if payload.relocation_summary:` guard (line 573) should
already give this for free; just confirm it in the test.

---

## Task 14 [Minor] — record this remediation round

**Depends on:** all prior tasks (this is the closing bookkeeping task, dispatch last).

**Fix:** append an entry to `docs/PROGRESS.md` (this project's checkpoint log, per CLAUDE.md
Rule 10) summarizing: which `SECURITY_REVIEW.md` items were closed by which task/commit range,
which remain open or were deliberately disclosed-not-fixed (Task 5's Cargo gap), and the D-number
Task 8 allocated. Do not re-word or delete anything Task 8 already wrote in
`docs/INTEGRATION_HONESTY.md` or `docs/DECISIONS.md` — this task only adds the `PROGRESS.md`
checkpoint entry, per this project's own rule that a ledger/progress record is annotated forward,
never rewritten. Also update `SECURITY_REVIEW.md`'s own per-item Status lines (items #1, #2, #4,
#5, #6, #7) to reflect what actually landed, each with a dated note, not a silent rewrite of the
original investigation text — the file's own convention (see item #3's "CLOSED as originally
worded; reopened narrowly" style) is to append status corrections, not overwrite prior prose.

**Test:** none (documentation-only). Report should list every file this task touched and confirm
none of them are code files.

---

## Task Ordering

Sequential (never parallel implementers, per the SDD skill): **1, 2, 3, 4, 5, 6, 7, 9, 10, 11,
12, 13, 8, 14** — Task 8 moves after 6/7 (its dependency) but before 14 (which summarizes
everything including Task 8's D-number); Task 13 immediately follows Task 12.

## Final Review Scope

The whole-branch review (after Task 14) should re-run the full test suite once (`pytest`, ~15
min, background per CLAUDE.md §6 — green means `xfail: 0` with every xfail disclosed by D-number)
and re-check the specific claims Task 8 corrected against the code Tasks 6/7 actually shipped,
per this project's own "re-run the check against the artefact the fix produced, not against the
finding" discipline (CLAUDE.md Guardrail 6).
