# HOOK1 report — pre-commit enforcement of the agent-worktree/branch correspondence

## STATUS

DONE. Committed, verified by direct execution (not assumed), teaching-message refusals confirmed
in all required paths, no unrelated files touched.

## Commit

`7a8bfbb` on `main`, in the primary checkout, pathspec-scoped to exactly:
- `.githooks/pre-commit` (new)
- `tools/worktree/install-hooks.sh` (new)
- `docs/DECISIONS.md` (append)

`git status --short` immediately before staging showed no other tracked changes in the working
tree; `git add .githooks/pre-commit tools/worktree/install-hooks.sh docs/DECISIONS.md` (no `-A`,
no `.`) staged only those three paths, confirmed with `git status --short` before commit.

## ADR

**ADR-0074**, appended to `docs/DECISIONS.md` (file ran to ADR-0073 at start of this task).
Title: "The agent-worktree/branch correspondence is enforced by a `pre-commit` hook, not a brief:
`.githooks/pre-commit` refuses a commit whose branch and checkout side disagree, installed via
`core.hooksPath` set to an **absolute** path into the primary because a relative one silently
reduces to no enforcement at all."

## The rule implemented

- Linked worktree → commit must be on a branch matching `agent/*` (never `main`, never anything
  else, never detached).
- Primary checkout → commit must NOT be on an `agent/*` branch. `main` (or any other non-`agent/*`
  name) is unrestricted there.

**Orchestrator's question, answered directly: no override is needed for `main` in the primary.**
That is the default-allowed path under the rule above — it was never blocked. An escape hatch
(`HARNESS_ALLOW_BRANCH_OVERRIDE=1`, prefixed on the commit invocation, not exported, not `FLEET_*`)
exists only for the two cases the rule *does* block, for a human operator's deliberate emergency
use. It prints a visible stderr line when used and is not something a subagent brief should ever
instruct.

## Design decisions and what was verified (not assumed)

1. **Primary-vs-worktree detection**: `git rev-parse --absolute-git-dir` vs. `git rev-parse
   --git-common-dir` (resolved to absolute via `cd`). Identical in the primary, different in a
   linked worktree. Same comparison `tools/worktree/new-worktree.sh` already uses (reused, not
   invented). Measured directly:
   ```
   # primary
   $ git rev-parse --absolute-git-dir
   /home/redmage/swe repo harness/.git
   $ git rev-parse --git-common-dir
   .git
   # linked worktree (worktrees/wt-WT1-example)
   $ git rev-parse --absolute-git-dir
   /home/redmage/swe repo harness/.git/worktrees/wt-WT1-example
   $ git rev-parse --git-common-dir
   /home/redmage/swe repo harness/.git
   ```

2. **Hooks fire in linked worktrees by default** (no `core.hooksPath` needed for that much):
   dropped a throwaway `.git/hooks/pre-commit` (`exit 1` unconditionally) in the primary,
   committed from the linked worktree → fired: `HOOK FIRED in
   /home/redmage/swe repo harness worktrees/wt-WT1-example`, exit 1. Confirms hooks are read from
   the **common** dir by default.

3. **`core.hooksPath` propagates via the shared `.git/config`**: set from the primary, immediately
   visible and effective from the already-existing linked worktree with no per-worktree step —
   confirmed both by `git config core.hooksPath` returning the same value from both locations and
   by a live commit firing the pointed-to hook from the worktree.

4. **Sharpest finding — a missing `core.hooksPath` target is a SILENT, TOTAL bypass**: pointed
   `core.hooksPath` at a nonexistent directory and committed. No error, no message, `pre-commit`
   simply never ran, commit succeeded, exit 0. This directly drove the decision to install an
   **absolute** path into the primary's `.githooks/` rather than the more idiomatic repo-relative
   `.githooks` — a relative path resolves against whatever the *committing worktree's own branch*
   has checked out, and an agent branch cut before this hook merged (the normal case for a
   short-lived task branch) would hit exactly this silent-bypass condition. Documented as a
   real, named tradeoff in ADR-0074 §3 (stale-checkout risk if the primary itself moves/renames —
   louder and rarer than the relative-path failure mode).

5. **Fail-closed**: every git command the hook depends on is wrapped so a command failure refuses
   the commit (prints the failing condition, points at the override) rather than defaulting to
   allow.

## Verification matrix (all exercised for real, throwaway files/branches, cleaned up after)

Explicit pathspecs were used throughout — this ran inside the *live* primary checkout while a
sibling agent had `tools/worktree/README.md` / `land-worktree.sh` staged concurrently; the whole
point of this task is to not repeat the incident it exists to prevent.

| # | Location | Branch | Expected | Actual result |
|---|----------|--------|----------|----------------|
| A | linked worktree (`wt-WT1-example`) | `agent/WT1-example` | allowed | **Allowed.** Commit `1dc9397` created, cleaned with `git reset --soft HEAD^` + unstage + rm (worktree was fully clean before/after). |
| B | linked worktree | `not-an-agent-branch-2` (non-`agent/*`; `main` itself can't be checked out there — already checked out in primary, git refuses two worktrees on one branch) | refused | **Refused**, exit 1, teaching message shown below, no commit created. |
| C | primary | `main` | allowed | **Allowed.** Commit `49341fa` created, undone with `git revert --no-edit` → `6cf9ff7` (chosen over history rewrite because a sibling lane was committing concurrently). |
| D | primary | `agent/hook-test-primary` (new branch, not checked out elsewhere) | refused | **Refused**, exit 1, teaching message shown below, no commit created; branch deleted after. |
| E | linked worktree | non-`agent/*`, with `HARNESS_ALLOW_BRANCH_OVERRIDE=1` | allowed via override | **Allowed**, printed `pre-commit: HARNESS_ALLOW_BRANCH_OVERRIDE=1 set -- branch/worktree guard skipped.`, commit `a76a12f`, branch deleted after. |

Test B's actual output (case D's is structurally identical, primary side):

```
pre-commit refused: commit on branch 'not-an-agent-branch-2' inside a linked worktree.

Every linked worktree is a subagent's isolated lane. This project's rule (docs/DECISIONS.md
ADR-0074) is that a lane commits only on a branch matching 'agent/*' -- never 'main', never
anything else, never detached -- so that one agent's work cannot land on a branch another agent
or the orchestrator is relying on. What to do instead:
  * Check out this worktree's own agent/* branch (tools/worktree/new-worktree.sh created one
    named 'agent/<task-id>' for you): 'git checkout agent/<task-id>'.
  * If you actually meant to touch 'main' or another shared branch: that belongs in the PRIMARY
    checkout (/home/redmage/swe repo harness), not this worktree.
  * Emergency-only override (docs/DECISIONS.md ADR-0074): re-run with
    HARNESS_ALLOW_BRANCH_OVERRIDE=1.
```

Test D's:

```
pre-commit refused: commit on branch 'agent/hook-test-primary' from the PRIMARY checkout (/home/redmage/swe repo harness).

'agent/hook-test-primary' is an agent lane branch, and its commits belong in that agent's own linked worktree
-- not in the orchestrator's primary checkout, even if that branch happens to be checked out
here right now. What to do instead:
  * If this is genuinely orchestrator work: commit it on a non-'agent/*' branch (main is the
    normal case) -- that is what the primary checkout is for.
  * If this is agent work: 'cd' into that agent's own worktree (see
    tools/worktree/new-worktree.sh and tools/worktree/README.md) and commit there.
  * Emergency-only override (docs/DECISIONS.md ADR-0074): re-run with
    HARNESS_ALLOW_BRANCH_OVERRIDE=1.
```

Repo state after all tests: `git status --short | grep -v '^??'` empty in both the primary and the
linked worktree; `git log --oneline` shows only the two throwaway-commit/revert pairs (`49341fa`
+ `6cf9ff7`) plus the real deliverable commit `7a8bfbb` — no test branches, no leftover files.

## Near-incident during verification (disclosed, not hidden)

The throwaway `.git/hooks/pre-commit` stub used in step 2 above (`echo … >&2; exit 1`,
unconditional) was left in place — untracked, in `.git/hooks/`, not `.githooks/` — while other work
continued in this shared primary checkout. It was masked from most activity because
`core.hooksPath` was already pointed at `.githooks/` by the time other work resumed, but **it did
block at least one legitimate commit from the concurrent `tools/worktree/` lane** before the
orchestrator flagged it. That agent retried rather than reaching for `--no-verify` — to its credit,
and worth noting since a different agent bypassing hooks on first contact would have taught it that
bypassing hooks works, which is the opposite of this task's intent.

**Removed**: `rm -f "/home/redmage/swe repo harness/.git/hooks/pre-commit"`, confirmed only
`*.sample` files remain in `.git/hooks/`. This file was always untracked (never part of any commit,
never at risk of shipping), but it was live in this working copy of the primary and is called out
here per Guardrail 6 — an unmeasured "clean" claim is worse than a disclosed near-miss.

## What this hook cannot enforce — for the next brief to cover

- **`git add -A` / any broad-pathspec stage.** The hook checks branch name and checkout side only;
  it never reads `git diff --cached`. An agent that stages a sibling's edits alongside its own, on
  a correctly-named `agent/*` branch, in its own worktree, produces a commit this hook allows
  without comment. This is exactly the shape of 3 of the 4 incidents that motivated this task and
  is **not** covered.
- **A pathspec-less `git commit`.** Same root cause — no diff inspection.
- **An agent editing files outside its declared lane.** No mechanism here checks *which* files
  changed against *which* lane a brief assigned; that remains prompt-only discipline.
- **Its own installation.** `core.hooksPath` unset, or pointed elsewhere, or `.githooks/pre-commit`
  deleted → every commit succeeds with zero indication enforcement is off (§ finding 4 above). No
  script running as the hook can defend against the hook not being invoked at all.
- **`--no-verify`.** Bypasses every git hook unconditionally; nothing here can see it coming or
  refuse around it.

ADR-0074 §6 states all of this in the ledger itself, deliberately, so it is not left implicit for
a future brief to overclaim past.

## Concerns for the orchestrator

- Confirm the sibling `tools/worktree/` lane's `land-worktree.sh` (and any future onboarding docs)
  tell a **new** agent to run `tools/worktree/install-hooks.sh` once from the primary — it is local
  config, and `new-worktree.sh` provisioning does not run it automatically (out of this task's lane
  to add, flagged here instead).
- The gaps in the "what this hook cannot enforce" list above are real and current — any brief
  claiming "lane isolation is enforced" without qualifying it to the branch/checkout axis
  specifically would be overclaiming, per this task's own explicit warning against that.
