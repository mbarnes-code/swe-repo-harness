# Proposed additions to `CLAUDE.md` — from round B (8 lanes, 7 fix rounds deep)

Twelve rules earned a place. Each cost time or nearly shipped a defect in round B, changes what an
agent *does*, and is not already in `CLAUDE.md`. Ordered by expected value to a future agent, not
by when it happened. Rejected candidates and proposed placement are at the end.

---

## 1. Prove a rewritten test is stronger with an old-passes / new-fails pair on the same input

Showing the new test fails under a mutation proves nothing about the old one. `CLEAN1` demonstrated
the gap: under a mutation where an earlier gate fires first, the pre-fix assertion
(`"pip install" not in msg`) **passed** while the new one (`"anthropik" in msg`) **failed** — the
only evidence that the rewrite was stronger rather than merely different. Two refinements the round
added, both from lanes catching their own false claims: when several mutations are cited, only the
**discriminating** one counts (FD1's fresh-uid mutation killed the old and new bodies alike and
proved nothing; the discriminating mutation was removing the `(run_id, event_uid)` dedup), and a
**mutation must be shown to have actually changed the code before its result means anything** —
CLEAN1's first mutation silently no-op'd because its regex missed a trailing comment, so the "pass"
it reported was worthless.

## 2. Verify the resolved value in the environment that will run it — never a stand-in for it

Nine separate findings this round were the same shape: a declaration read instead of a value
exercised. The orchestrator deleted `effort: low` from YAML believing the parameter would stop being
sent — `BackendTarget.effort` was non-optional with default `"medium"` (`models/tasks.py:91`), so the
edit substituted a value the operator never wrote. BK1 claimed a "when non-empty" gate that the type
system made unconditional, because it read the field's declaration rather than constructing the
object. FD1 concluded a code path was unreachable by reasoning from *importer identity* to *call
path*, and it was live in shipped `fleet pr` (`cli.py:9149-9226`, worker run directly at `:9299`
with no `PhaseRunner`). Load the settings, construct the model, run the command.

## 3. Three different questions, three different probes: `find_spec` = installed · `sys.modules` = imported so far · `pyproject.toml` = declared

Three lanes lost time conflating two of these. Concretely: the **system** `python3` has `boto3` while
the project venv does not, so a probe run against the wrong interpreter *and* a read of
`pyproject.toml` both return the wrong answer to "can this import here?". BK3 hit the class twice one
round apart — after fixing a declared-vs-installed error it shipped an imported-so-far-vs-installed
one (a test stub guarded on `"requests" not in sys.modules`, which shadowed a really-installed
`requests` session-wide). Naming an error class does not inoculate against its variants; write the
probe down. **Note this amends existing Guardrail 2, which names `pyproject.toml` as the thing to
check** — correct for "what do we declare", wrong for "what is installed".

## 4. Mutation testing verifies the implementation, not the truth of a test's own name

A test can pass, and pass under mutation, while the property in its name is false — the mutation
perturbs only the path the test exercises, never the other doors to the same state. Demonstrated by
construction, not argued: `test_state_models.py:669-683` promised a function demotes *silently*; a
reviewer bound an audit side-effect into that function, left the return value alone, and **the test
stayed green while its name became false**. The overstated claim had already survived four
self-checks and a mutation test. When a test's name asserts an absence ("without recording",
"cannot be taken without"), the name needs its own proof.

## 5. Fix the code and the doc listing in the same change — a stale listing is what the next author reconciles against

Three lanes were bitten by this identical shape. `SPEC.md:2073-2074`'s §5.1 listing kept the
pre-change `transition()` docstring, deleting the very warning DEM1's entire remedy rested on — in
the one artifact the downstream subtask's author reads. `SPEC.md:2889` kept `effort: Literal[...] =
"medium"` after BK2 made the field optional, so a spec-driven reconciliation would have restored the
fabricated default and re-keyed the cache a third time. Code listings inside docs are not
documentation; they are inputs to future edits.

## 6. Sweep for the class, not the reported site — wrong text propagates by copy

The misleading comment that produced the same cache-key bug in three independent lanes existed in
**five** places (`models/tasks.py:47`, `state/schema.sql:434`, `SPEC.md:2845`, `SPEC.md:4164`, plus
two verbatim quotations inside another lane's files, one wrapped across two lines so a single-line
grep missed it). Every lane that touched it found one more. Same shape when BK2 was asked to fix two
stale `effort` sites and a sweep of all 33 occurrences found seven. Two corollaries: after any
renumber or rename, grep the whole tree (an ADR renumber touched 8 cross-references including code
comments and a test docstring); and when you *delete* a false claim, grep for what cited it — the
cache-determinism rationale rested on a premise BK2 removed, and survived only because a second,
code-backed leg existed at `SPEC.md:6812-6814`. Counter-caveat, seen the same round: **editing a
correct sentence because it matched your grep is the mirror-image error** (`SPEC.md:3275` uses the
same phrase correctly; it was reported, not edited).

## 7. "The SPEC says X but the code cannot do X" is two edits, not one

Deciding the code is half the adjudication; the SPEC sentence must be corrected in the same breath
or it regenerates the defect. This was the orchestrator's own error and it stood the entire round:
BK1's round-1 concerns named `SPEC.md:5601`'s claim that the backend uses effort "where the target's
declared capabilities carry it" when `ModelCapabilities` has no such field. The implementation
question was adjudicated; the sentence was left. It resurfaced as a land-blocking finding six rounds
later, with a concrete failure — a reconciler adds `supports_effort` to make code match spec and
suppresses an explicitly declared value.

## 8. After fixing an overclaim, re-run the original detector against the fix

The round's most reliable pattern: a fix for an overclaim introduces a narrower overclaim, always a
scope or probe narrowing of the one just fixed. BK2 twice (a corrected claim about `src/` that was
itself false; then a NOT-IMPLEMENTED marker whose *scope line* excluded the exact claim it existed
to neutralise, at `DECISIONS.md:296` vs `:291`), FD1 once (a caveat written unconditionally that
asserted every row has scope `"run"`, contradicting its own `scope` field), BK3 once (the probe error
above). "Be careful" did not work; re-running the original detector against the fix is what caught
both of the last two.

## 9. Validate an instrument against a known-bad state before trusting a clean result

A detector that has never been observed firing is not evidence of absence. BK2 ran its markdown
parser against the known-broken commit first, confirmed it reported the absorbed line, and only then
cited a clean post-fix render. CLEAN1 went one better and validated three ways: fires on the
known-bad state, silent on an already-swept file, and **fires on a synthetic fault injected into a
clean one** — the third check is the one that catches a detector that is silently broken on fresh
instances. The same discipline caught a mutation harness that was not mutating (lesson 1).

## 10. For any claim about a file a sibling lane owns, anchor to the sibling's branch, not `HEAD`

`HEAD` is main, where the sibling has not landed. A reviewer followed a brief that said to anchor
findings with `git show HEAD:` and produced a **false** finding — the field it flagged as
non-optional was already optional on the sibling's branch. Anchoring to `HEAD` is right for
main-state claims and wrong for cross-lane ones; briefs must distinguish the two and name the ref.
A related coupling worth avoiding entirely: **quoting another module's comment verbatim** creates a
cross-file dependency nothing enforces — when the original is reworded, the quotation points at a
string no longer in the tree and a reader concludes the surrounding warning is stale.

## 11. Assign ADR (and any shared append-only) numbers centrally at dispatch

Two lanes both wrote ADR-0075 because main was at 0074 and each independently took "the next
number". Neither could see the other; no agent could have caught it, only the coordinator, and it
surfaced as a land blocker found by diffing `docs/DECISIONS.md` across five worktrees. Expect the
shared doc not to auto-merge when three lanes append to its tail.

## 12. Distinguish an adversarial-only escape from an accidentally-reachable one; document the first, fix the second

When a reviewer shows your test can be defeated, the right question is whether a normal author would
trip it. DEM1's test could be defeated seven ways, all requiring a deliberately side-effecting
subclass — documented as a stated boundary and deliberately **not** patched, because patching would
have bought the appearance of closure (four of the escapes bypassed the mechanism a patch would
harden). The single *accidentally* reachable weakness got fixed. Two supporting rules from the same
thread: prefer inverting an enumeration of escapes into a whitelist (a side effect must *name*
something to reach it, so unpredicted forms trip it too) over extending the blacklist a fourth time;
and never close a documentary gap with "a convention wearing a mechanism's clothes" — a fake
mechanism is worse than an honest disclosure because it *looks* enforced.

---

## Candidates rejected, and why

**Already covered by `CLAUDE.md`:**
- *"Never pass an unmeasured number into a doc; re-measure"* — Guardrail 6, first bullet. The round
  supplied several fresh instances (a test count of 36 that existed at no commit; a "58 hunks"
  claim that was 59) but no new rule.
- *"Re-survey worktrees after an interruption; agent reports describe intent, `git status` describes
  reality"* — Guardrail 6, second bullet, already covers the audit-vs-uncommitted hazard.
- *"Don't add a dependency on agent authority; don't launder an agent's recommendation into a
  directive"* — Guardrail 1. It held all round; a lane recorded an orchestrator decision as an
  orchestrator decision, correctly.
- *"Verify third-party facts against the codebase before citing them"* — Guardrail 2. Lesson 3
  amends it rather than restating it.
- *"Tests must verify why logic matters"* — Rule 9. Lessons 1 and 4 are about *proving* a test does
  that, which Rule 9 does not address.

**Below the bar / wrong document:**
- *"Use `general-purpose`, not `Explore`, for any agent expected to write a file."* True and it cost
  a manual persist of a research report, but it is dispatch mechanics rather than an engineering
  rule, and the failure is self-correcting at zero code risk. Belongs in the SDD round template.
- *"Commit early; a session limit can kill a lane with real code only in the working tree."* Two
  lanes were in that state at the kill. No work was lost, and Rule 10 already establishes checkpoint
  discipline in a different medium.
- *"Don't edit a file while a review of it is in flight; batch the change."* Real (avoided twice,
  deliberately) but it is scheduling, and it only applies to a multi-lane review pipeline.
- *"An observability write must never be able to take down the path it observes."* Three instances in
  one lane, genuinely load-bearing — but it is a design constraint for one subsystem, now recorded in
  that lane's code and ADR, not agent behaviour.
- *"A bare set-equality assert with no failure message invites the wrong fix"* (an author trips it and
  widens the set to go green). Sharp, but it is a code-review checklist item.
- *"An implementer's 'this defect is pre-existing' claim is exactly the claim a reviewer must check."*
  True, and it was the root of a Critical — but it is an instance of lesson 2, not a separate rule.

## Proposed placement

| Lesson | Section |
|---|---|
| 1 (old-passes/new-fails), 4 (mutation ≠ the name) | §4 Core Engineering Directives — new **Rule 12**, immediately after Rule 9 ("Tests Verify Intent"), which they operationalise |
| 2 (resolved value, not a stand-in) | Guardrails — extend **#6**, retitling it *Measurement & Stand-In Discipline*; it is the same family as the existing unmeasured-number bullet |
| 3 (three-way probe) | Guardrails — amend **#2** *Fact-Checking Reference Material*; the current text names `pyproject.toml`, which lesson 3 shows is the wrong probe for "installed" |
| 5, 6, 7 (stale listings, sweep the class, SPEC-and-code together) | Guardrails — **new #7, "Documents Are Inputs to Future Edits"**; all three share the premise that a doc is what code gets reconciled against |
| 8 (re-run the original detector), 9 (validate the instrument) | Guardrails — extend **#6** alongside lesson 2 |
| 10 (sibling branch ref), 11 (central ADR numbers) | §3 SDD Protocol — both are obligations on the dispatching orchestrator, not on workers |
| 12 (adversarial vs accidentally reachable) | §4 — fold into the same **Rule 12** as lessons 1 and 4; it is the stop rule for the hardening loop those two start |
