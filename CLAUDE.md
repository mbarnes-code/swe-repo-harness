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
Tests must verify *why* logic matters (e.g., ensuring a failed build increments a retry counter in `migration_state.json`).

### Rule 10 — Checkpoint After Every Step
After every major component completion, log progress in `docs/PROGRESS.md` detailing: *What was completed*, *What was verified*, and *Next subagent task*.

### Rule 11 — Fail Loud
Never hide errors. If a build or AST transformation fails after 3 subagent retries, mark the target repo as `REQUIRES_HUMAN_INTERVENTION` in `migration_state.json` and move to the next item.

---

## 5. Workspace Directory Layout
- `references/` — **READ-ONLY.** Never modify.
- `docs/` — Specifications (`SPEC.md`), decisions (`DECISIONS.md`), progress logs (`PROGRESS.md`).
- `src/` — Executable Python codebase (`asyncio`, `Pydantic v2`).
- `tests/` — Automated test suites.

## Architectural & Subagent Guardrails

1. **Directive Authority & Lineage (No Laundering)**
   - Subagents must NEVER cite a constraint as "by directive," "mandatory," or "out of scope" unless that constraint explicitly exists in `CLAUDE.md` or a primary reference document.
   - Design choices originating from an agent must be explicitly labeled as *Agent Recommendations*, never as hard system requirements.

2. **Fact-Checking Reference Material**
   - Before referencing third-party dependencies, reference harnesses, or file contents in specs and ADRs, verify the facts directly against the codebase (`pyproject.toml`, manifests, imports). Never cite assumed dependencies.

3. **Interface-First External Services (Dependency Inversion)**
   - High-level orchestration engine logic must never depend directly on concrete vendor SDK singletons. Always route third-party APIs (LLM providers, external tools) through lightweight Python `Protocol` interfaces (e.g., `ModelClient`).

4. **Strict State Management Boundaries**
   - Git is the sole source of truth for code, tree SHAs, diffs, and worktree rollbacks. 
   - Databases (SQLite/Postgres) are strictly restricted to execution state: task queues, step statuses, worker heartbeats, and timestamp logging. Do not build shadow version-control systems.

5. **Fresh Context on Error Retries**
   - Repair loops and LLM re-prompts must provide fresh, verbatim error logs (`stderr`, build output) and target source files rather than appending full transcript histories or failed diff patches.
