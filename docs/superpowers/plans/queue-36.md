# Plan: checkpoint 36 — D34 fallout, publish-path recovery, runner invariant

Source: `docs/PROGRESS.md` §35, `docs/INTEGRATION_HONESTY.md` D21–D45, `docs/SPEC.md`.
Base commit: `68a41ff`.

## Global Constraints

- **No agent may run `pytest`.** `tests/conftest.py::pytest_sessionfinish` deletes every
  `BAZEL_ROOT` child except `repos/`, so any session that finishes destroys a concurrently
  running one. The orchestrator serializes all suite runs. Write tests; do not execute them.
- Never export `FLEET_*` (settings uses `env_prefix="FLEET_"` + `extra="forbid"` → exit 2).
- `references/` is READ-ONLY. No `sudo`. Stay inside the project directory.
- Surgical changes only (CLAUDE.md Rule 3). Touch only the files your task owns.
- Any claim entering `docs/` must be measured, not assumed (Guardrail 6).
- Tests verify *why* logic matters (Rule 9), not merely that a line executes.

## Task 1 (Worker A) — Docker invocation identity and the 125 branch

Owns: `src/fleet/workers/buildverify.py` and the docker sandbox argv construction.

1. `--name=` collision: a retry reuses the identical container name, so when a dead daemon
   leaves the container behind, the retry's 125 is *permanent*. Replace with a
   per-invocation token plus prefix-based removal. This gives `list_by_prefix` its first
   caller and closes half of D32.
2. `_DOCKER_CANNOT_RUN_EXPLAINED` asserts a retry-ladder outcome ("no attempt charged") that
   is false past the 4-retry cap. Correct the message to what the ladder actually does.
3. `_c_toolchain_gate` has no clock branch: a >120 s image pull currently yields an immediate,
   permanent, fabricated "install gcc". Give it one.

## Task 2 (Worker B) — D26 then D27, in that order

Owns: `src/fleet/cli.py` publish path and `src/fleet/vcs/git.py`.

D26: `_publish`'s dirty-guard asks the whole worktree (`git status --porcelain`) rather than
the paths it is about to publish, so unrelated dirt blocks publication. D27: the module lock
is compared against a file rather than the branch, so re-entry republishes spuriously.
D26 fires first and *blocks D27 from being observable*, so fix D26 first, then D27.

## Task 3 (Worker C) — ScriptedRunner invariant and the three raw-exec sites

Owns: `src/fleet/util/proc.py` (invariant + anti-drift test) and `src/fleet/vcs/github.py`,
`src/fleet/vcs/gitea.py`, `src/fleet/vcs/filter_repo.py`.

`ScriptedRunner` may currently emit states `util.proc.run` cannot produce. Derive the
invariant from `util/proc.py` and enforce it with an anti-drift test. Then: exit 127 is dead
code at all three sites, `not started` matches only a passed deadline, and a genuinely missing
binary escapes as a raw `FileNotFoundError`.

## Task 4 (Research) — routing questions for the workers

No code. Answer in a report file, with sources and measured commands where possible.

## Task 5 (Review) — audit `44d5550..68a41ff`

No code. The D34 overstatement was caught only after it was committed; find what else in that
range overstates its evidence.
