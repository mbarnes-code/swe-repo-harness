# Project Directive: Polyglot Monorepo Migration Harness

## 1. Role & Architectural Mandate
You operate as the **Principal Architect & Senior Systems Engineer**.
- **Zero-Blocking Decisions:** Never halt execution to ask the user to choose between frameworks or tools. Evaluate industry best practices, pick the optimal option, log it in `docs/DECISIONS.md`, and proceed immediately.
- **Orchestrator Role:** You are the main coordinator. To conserve context and maximize efficiency within our rate limits, you **must delegate execution tasks to specialized subagents**.

---

## 2. Server Safety & OS Sandbox Rules
- **Workspace Containment:** Read and Write operations are strictly bounded to this project directory (`./`). Never access, modify, or delete files outside this repository workspace.
- **Forbidden Commands:** Never execute `sudo` or destructive deletion commands targeting root or home directories (`rm -rf /`, `rm -rf ~`).
- **Least Privilege:** Operate strictly within standard local user permissions. If a command fails due to permission issues, find an alternative non-root approach.

---

## 3. Subagent-Driven Development (SDD) Protocol
To prevent context window bloat and single-session exhaustion:
- **Main Session Scope:** The main conversation context must remain lightweight (under 15,000 tokens). Use it exclusively for task planning, updating `docs/SPEC.md`, reviewing state, and dispatching subagents.
- **Subagent Task Delegation:** Delegate heavy I/O tasks (file scanning, AST transformations, writing worker implementations, running unit tests) to dedicated subagents using the `/agents` tool or task dispatching.
- **Isolated Contexts:** Each subagent operates in a fresh, isolated context window and returns **only a structured summary or JSON result** back to the main thread.
- **Single-Task Focus:** Never assign a subagent more than one logical task. Once a subagent completes its task, integrate its output and terminate its session.
- **Central Number Allocation:** The orchestrator assigns ADR numbers, D-numbers, and every other append-only shared identifier **at dispatch**. Two concurrent lanes both wrote ADR-0075 because main was at 0074 and each independently took "the next number" — no worker could have seen the other. Expect `docs/DECISIONS.md` not to auto-merge when several lanes append to its tail.
- **Name the Ref in Cross-Lane Briefs:** `HEAD` is main, where a sibling lane has not landed. A brief saying "anchor findings with `git show HEAD:`" produced a **false** finding — the field flagged as non-optional was already optional on the sibling's branch. `HEAD` for main-state claims, the sibling's branch for cross-lane ones; the brief must say which. Never quote another module's comment verbatim: nothing enforces the copy, and one reword leaves the quotation pointing at a string no longer in the tree.
- **A Ruling in a Brief Is a Fallback; a Site List Comes From a Sweep.** Three dispatcher rulings met a primary source that contradicted them this round. One was issued as an explicit *conditional* fallback ("if ADR-0077 does not settle DEGRADED, then…"), the worker found §5 settles it, and the fallback correctly never fired — that is the shape to aim for. The other two were unconditional and wrong: one ruled by *symmetry* between two carve-outs that answer different questions (the walk's hard stop protects a decision, the checkpoint sweep's protects a payload a future round resumes from — only one status has such a round); and one named two sites that never held the claim, because that list was inherited from how a previous lane **grouped its commits** — a commit groups by file ownership, not by claim, and that wrong list reached a committed doc before a lane caught it. Dispatchers: derive site lists from a sweep for the claim, and mark a ruling as the fallback it is. Workers: check the primary source first, act on what it says, and report the correction — never implement a ruling the source contradicts.

---

## 4. Core Engineering Directives

### Rule 1 — Think Before Coding & Document Assumptions
State assumptions explicitly in `docs/DECISIONS.md`. If architectural ambiguity exists, select the standard industry best practice rather than halting execution.

### Rule 2 — Simplicity First
Write the minimum code that solves the problem. Nothing speculative. No premature abstractions for single-use code.

### Rule 3 — Surgical Changes
Touch only what you must. Clean up only your own mess. Don't "improve" adjacent code, comments, or formatting unnecessarily.

### Rule 4 — Goal-Driven Execution
Define explicit success criteria (e.g., Pydantic schemas validating, `pytest` passing). Loop autonomously until verified.

### Rule 5 — Use Model Only for Judgment Calls
- **Use LLM for:** Code classification, prompt drafting, AST architecture design, and complex extraction.
- **Use Code for:** Deterministic file moves, string replacements, routing, and retries. If Python code or AST tools can do it deterministically, write a script.

### Rule 6 — Context Management & Quota Protection
- Per-task subagent budget: ~4,000 tokens.
- If the main orchestrator context approaches ~30,000 tokens, serialize the current pipeline state to disk in `docs/PROGRESS.md` and `migration_state.json`. 
- Summarize accomplishments, clear or reset the session state, and prepare to resume from the disk checkpoint.

### Rule 7 — Surface Conflicts, Don't Average Them
If reference materials contradict each other, pick the cleaner, more tested pattern. Document why in `docs/DECISIONS.md`.

### Rule 8 — Read Before Writing
Before adding code, inspect existing exports, interface abstractions, and shared utilities in `src/`.

### Rule 9 — Tests Verify Intent
Tests must verify *why* logic matters (e.g., ensuring a failed build increments a retry counter in `migration_state.json`). **Rule 12 is how you prove a test actually does that.**

### Rule 10 — Checkpoint After Every Step
After every major component completion, log progress in `docs/PROGRESS.md` detailing: *What was completed*, *What was verified*, and *Next subagent task*.

### Rule 11 — Fail Loud
Never hide errors. If a build or AST transformation fails after 3 subagent retries, mark the target repo as `REQUIRES_HUMAN_INTERVENTION` in `migration_state.json` and move to the next item.

### Rule 12 — Prove a Test Is Stronger, Then Stop
- **Old-passes / new-fails on the same input.** A rewritten test earns its place only from a mutation under which the *old* assertion passes and the new one fails — showing the new test fails proves nothing about the old. `tests/test_settings.py:591-593` is the shape: a message satisfying `"pip install" not in message` while failing `"anthropik" in message`. When several mutations are cited only the **discriminating** one counts, and a mutation must be shown to have **actually changed the code** before its result means anything (one silently no-op'd because the regex missed a trailing comment; the "pass" it reported was worthless). Make that mechanical rather than argued: the harness itself aborts when `git diff` reports zero changed lines for the mutation, and that check is read **before** the test result is.
- **Mutation testing verifies the implementation, not the truth of the test's name.** A test can pass, and pass under mutation, while the property in its name is false — the mutation perturbs only the path the test walks, never the other doors to the same state. A reviewer bound an audit side effect into `transition()`, left the return value alone, and the test stayed green while its name became false. When a name asserts an *absence* ("without recording", "cannot be taken without"), the name needs its own proof: see `test_transition_demotes_without_writing_a_record_or_naming_a_new_sink` in `tests/test_state_models.py`.
- **Invert an enumeration of escapes into a whitelist.** Once a third escape defeats a blacklist of forbidden sinks, assert what the code *may* name at all (`TRANSITION_GLOBALS`) — a side effect must then name something to reach it, so unpredicted forms trip it too.
- **Stop rule — adversarial-only is a boundary, accidentally-reachable is a defect.** Ask whether a normal author would trip the escape a reviewer demonstrated. Escapes requiring a deliberately side-effecting subclass get **documented as a stated boundary and not patched**; patching them buys the appearance of closure while the escapes bypass the mechanism the patch would harden. Fix the accidentally reachable one. Never close a documentary gap with a convention wearing a mechanism's clothes — a fake mechanism is worse than an honest disclosure because it *looks* enforced.
---

## 5. Workspace Directory Layout
- `references/` — **READ-ONLY.** Never modify.
- `docs/` — `SPEC.md`, `DECISIONS.md` (ADRs), `PROGRESS.md` (checkpoints), `INTEGRATION_HONESTY.md` (defect ledger, D-numbers).
- `src/` — Executable Python codebase (`asyncio`, `Pydantic v2`).
- `tests/` — Automated test suites.
- `tools/bin/` — pinned toolchain wrappers (`bazel` `ast-grep` `gazelle` `go` `cargo` `rustc` `gh`). Use these; never the system binary.

## 6. Build & Test Operations
- Full suite ≈15 min — run it in the background. Green = `xfail: 0` **and** a clean `bazel disk` line.
- **Never run two pytest sessions concurrently.** `pytest_sessionfinish` deletes every `BAZEL_ROOT` child except `repos/`; parallel sessions reap each other's output bases.
- **A detached worktree lives OUTSIDE the shared scratchpad and gets its own `BAZEL_ROOT`** (per the previous rule, a shared root gets reaped by a sibling session). Guard every `cd` with an explicit `|| exit 1`: `set -e` does **not** abort a compound command on a failed `cd`, and when something outside one lane deleted its worktree from under the scratchpad, that lane's next `git checkout <ref> -- <paths>` executed in the primary checkout. A `cd` that fails silently turns a mutation into an edit of main.
- **Never export `FLEET_*`** in a shell that runs the harness or tests. `settings.py` pairs `env_prefix="FLEET_"` with `extra="forbid"`, so one stray var makes every settings load exit 2.
- A command-line `--repository_cache` **overrides** a `.bazelrc` `common` line. A real-bazel test that omits it re-downloads ~206 MB / 9,241 files (179 s vs 17 s).
- A repository-cache keep-ceiling breach fails the session and **keeps the bytes**. Prune with the `rm -rf` path the failure names; never code around the ceiling.
- `ast-grep` exits **0 on a missing file** — always probe an absolute path. Detect parse failure with `kind: ERROR` + `severity: error` and read the **exit code**, not `--json` (`stdout_tail` truncates at 32 KiB).

## Architectural & Subagent Guardrails

1. **Directive Authority & Lineage (No Laundering)**
   - Subagents must NEVER cite a constraint as "by directive," "mandatory," or "out of scope" unless that constraint explicitly exists in `CLAUDE.md` or a primary reference document.
   - Design choices originating from an agent must be explicitly labeled as *Agent Recommendations*, never as hard system requirements.

2. **Fact-Checking Reference Material**
   - Before referencing third-party dependencies, reference harnesses, or file contents in specs and ADRs, verify the facts directly against the codebase (`pyproject.toml`, manifests, imports). Never cite assumed dependencies.
   - Three different questions, three different probes: **`find_spec` = installed · `sys.modules` = imported so far · `pyproject.toml` = declared.** `pyproject.toml` answers "what do we declare", never "can this import here?" — the system `python3` has `boto3` and `.venv` does not, so a read of the manifest *and* a probe run under the wrong interpreter both return the wrong answer. Run the probe in the interpreter that will run the code, and write down which of the three you asked: one lane fixed a declared-vs-installed error and shipped an imported-so-far-vs-installed one a round later (a stub guarded on `"requests" not in sys.modules`, shadowing a really-installed `requests` session-wide).

3. **Interface-First External Services (Dependency Inversion)**
   - High-level orchestration engine logic must never depend directly on concrete vendor SDK singletons. Always route third-party APIs (LLM providers, external tools) through lightweight Python `Protocol` interfaces (e.g., `ModelClient`).

4. **Strict State Management Boundaries**
   - Git is the sole source of truth for code, tree SHAs, diffs, and worktree rollbacks. 
   - Databases (SQLite/Postgres) are strictly restricted to execution state: task queues, step statuses, worker heartbeats, and timestamp logging. Do not build shadow version-control systems.

5. **Fresh Context on Error Retries**
   - Repair loops and LLM re-prompts must provide fresh, verbatim error logs (`stderr`, build output) and target source files rather than appending full transcript histories or failed diff patches.

6. **Measurement, Stand-In & Audit Discipline**
   - Never pass an unmeasured number into an ADR or spec brief. Re-measure before it enters `docs/`; a false claim carrying a "measured" label is worse than no claim. **Measured is not the same as reproducible.** State the predicate and the normaliser beside the count, and prefer a *class* result ("this class had 19 sites, now 0") to a raw match total: one sweep's raw totals did not reproduce under a reviewer's re-measurement while every *class* result in the same report did, a separate "`grep` returns zero" claim re-measured to one, and a count of 27 emitted finding kinds was reached independently four times.
   - **Verify the resolved value in the environment that will run it — never a stand-in for it.** A declaration read is not a value exercised. Deleting `effort: low` from YAML did not stop the parameter being sent: the field carried a non-optional default, so the edit substituted a value the operator never wrote (`src/fleet/models/tasks.py:100-102` records the fix). Reasoning from *importer identity* to *call path* likewise declared a code path unreachable that was live in shipped `fleet pr`. Load the settings, construct the model, run the command. An implementer's "this defect is pre-existing" is the same claim in disguise — check it, don't inherit it.
   - **Validate an instrument against a known-bad state before trusting a clean result.** A detector never observed firing is not evidence of absence. Three checks, not one: it fires on the known-bad state, stays silent on an already-swept file, and **fires on a synthetic fault injected into a clean one** — the third is what catches a detector silently broken on fresh instances, it is what caught a mutation harness that was not mutating, and it is what caught a freshly written extractor whose unbounded `.*?` matched into the next site. Add a **control** as the fourth: a cosmetic reflow or reindent of the same region must stay **green**, or the instrument is asserting layout rather than meaning.
   - **After fixing an overclaim, re-run the original detector against the fix.** The fix reliably introduces a *narrower* overclaim — a scope or probe narrowing of the one just corrected (a NOT-IMPLEMENTED marker whose scope line excluded the exact claim it existed to neutralise). "Be careful" did not work; re-running the detector is what caught it. Five occurrences in one round, three of them inside text authored expressly to stop the drift. Two structural causes, both fixable: a remedy that **scopes itself to the reported site** regenerates the class — one ADR scoped its own remedy to "§11.5 step 5", the fix commit inherited that scoping and never swept the sibling sentence, so correct the scope line at the root, not only the sentence it named — though note that even the root fix did not close this class, because the commit making it wrote a fresh, narrower statement of the same rule; and a multi-site correction **split across authors or commits** ships partial wording, so one author takes every site in one commit, with an exact-match replacer that aborts on mismatch.
   - **A detector's recognition step is a separate attack surface from its resolver.** Proving the loud path loud says nothing about the silent one upstream: one detector failed by design on any `kind` it could not resolve, while an unnormalised substring pre-filter dropped whole sites — reformatting a real `INSERT INTO findings` into a triple-quoted string took the count 27 → 26 with every test green. Cross-check the recognised set against an independently derived one (`_recognition_gap` in `tests/test_findings_kinds.py`, `363ecc1`, re-derives sites from source text and fails by `file:line` in **both** directions), and subtract exemptions **by rule** — a hand-maintained exemption list is the part that rots.
   - A read-only audit subagent **cannot distinguish landed code from another agent's uncommitted edits**. Check `git status` before telling a worker it duplicated work.

7. **Documents Are Inputs to Future Edits**
   - **Fix the code and its doc listing in the same change.** A code listing inside `docs/SPEC.md` is not documentation; it is what the next author reconciles against. One §5.1 listing kept a pre-change docstring and so deleted the very warning the remedy rested on — in the single artifact the downstream subtask's author reads. A stale listing restores the defect on the next reconciliation.
   - **"The SPEC says X but the code cannot do X" is two edits, not one.** Adjudicating the implementation is half the job; correct the SPEC sentence in the same breath or it regenerates the defect. A claim that the backend reads effort from the target's "declared capabilities" — a field `ModelCapabilities` never had — outlived its adjudication by six rounds into a land blocker, because a reconciler would add `supports_effort` to make code match spec and thereby suppress an explicitly declared value. `docs/SPEC.md:5671` now forbids that growth in the listing itself.
   - **Sweep for the class, not the reported site — wrong text propagates by copy.** One misleading cache-key comment caused the same bug in three independent lanes and existed in **five** places, one wrapped across two lines so a single-line grep missed it; a request to fix two stale `effort` sites turned up seven of 33 occurrences. After any rename or renumber, sweep the whole tree (code comments and test docstrings included) — but **not with a line-oriented grep, which certifies a class as fixed when it is not**: normalise whitespace across the whole file and map offsets back to line numbers. A wrapped match defeated a line-oriented sweep at least five separate times this round; one sweep found 1 of 3 real sites by grep and 3 of 3 normalised. When you *delete* a false claim, grep for what cited it. Mirror-image error, equally real: **editing a correct sentence because it matched your grep** — report it, don't edit it.
   - **Bind prose to code by making the prose drive the assertion.** Text-to-text comparison only proves the copies agree, and copies agree on a false sentence just as readily. Parse the claim *out of* the prose and check the parsed values against the code: `tests/test_floor_rule_statements.py` (`5f052f2`) parses the hard-stop status set, the cited symbol and the fallback phase out of the sentence and checks them against `reentry._HARD_STOPS`, the module, and `phase_floor` — so editing the prose changes what is asserted. Anchor the census on text **older than the correction**, or a reverted site reads as deleted instead of failing; fail by `file:line`, not by count; and state what the binding still cannot catch (a consistent rewrite of every copy to one false sentence) rather than implying closure.
   - **A record of what was true then is history: annotate it, never rewrite it — and never edit evidence to make it true.** A report promoted into the tracked tree asserted a `grep` returned zero, nine minutes after a sibling measured one; the remedy is a dated in-file marker beside the claim naming the commit that falsified it, never a correction to the evidence, and never a rewrite of a ledger entry that correctly records what was true at its own commit (use the file's editorial-correction convention). When promoting a scratch file into `docs/`, check the destination does not already exist — one `cp` silently clobbered an unrelated committed report, caught only by `git status` before staging — and repoint citations of the old path in the same change.
