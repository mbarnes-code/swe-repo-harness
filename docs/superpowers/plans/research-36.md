# research-36 — Docker identity, replay scope, `build_diagnosis`, and the C-toolchain clock branch

Research round 36. Every numeric/behavioural claim below carries a label:

- **MEASURED** — I ran the command shown on this host and got the output shown.
- **INFERRED** — a conclusion drawn from measured facts plus code I read; not directly observed.
- **UNKNOWN** — I could not establish it; stated as such rather than guessed.

Host: Docker Server **29.7.2** (MEASURED — `docker version --format '{{.Server.Version}}'` → `29.7.2`).
Probe image for all Docker experiments: `debian:bookworm-slim` (present locally).
All test containers used the prefixes `resq1*` / `resq1p*` / `resq1d*` and were removed; final sweep
`docker ps -a --filter name=^resq1` returned empty and `--filter name=^fleet-` returned empty, i.e.
**no harness container was disturbed** (MEASURED). `pytest` was never invoked. No `sudo` was used.

---

## Q1 — Docker container identity under retry

### Q1.0 The headline: the incumbent proposal is already implemented in `buildverify`

**MEASURED (code read).** `src/fleet/workers/buildverify.py` already contains exactly the
"per-invocation token plus prefix-based removal" scheme the proposal describes:

- `_container_prefix(ctx)` — `buildverify.py:341-351`
  ```python
  return f"{sandbox_name(ctx.run_id, ctx.repo_id, ctx.attempt)}-t"
  ```
- `_invocation_name(ctx, *, suffix="")` — `buildverify.py:354-370`
  ```python
  return f"{_container_prefix(ctx)}{uuid.uuid4().hex[:8]}{suffix}"
  ```

Call sites (MEASURED): `buildverify.py:777` (`suffix="-cc-probe"`, the C-toolchain probe) and
`buildverify.py:1084` (`name=_invocation_name(ctx)` inside `_argv`).

**Correction to a claim circulating from the Q2 investigation:** the build and test steps do **NOT**
share an identical `--name`. `_argv` is invoked *per unit* inside the step loop
(`buildverify.py:934`, `argv = self._argv(ctx, payload, unit=unit, worktree=worktree)`), and each
invocation calls `_invocation_name(ctx)` afresh at `:1084`, minting a new `uuid4().hex[:8]`. So the
probe, the build and the test each get a distinct container name already. Anyone reading
`spec_for_attempt`'s *default* (`sandbox/container.py:114`, `sandbox_name(run_id, repo, attempt)`)
and stopping there will reach the opposite — and wrong — conclusion.

**Consequence for the blocked workers:** for `buildverify`, Q1's recommendation is largely a no-op;
the work is done. What remains are the four defects in Q1.5 below, two of which are real.

### Q1(a) Exit code and stderr from a `--name` collision

**MEASURED.** Three leftover container states were constructed and collided against:

| leftover state | how created | colliding `docker run --rm --name=<same>` |
|---|---|---|
| `created` (registered, never started) | `docker create --name X` | exit **125** |
| `exited` (ran without `--rm`) | `docker run --name X … true` | exit **125** |
| `running` | `docker run -d --name X … sleep 30` | exit **125** |

stderr is byte-identical in shape across all three (MEASURED):

```
docker: Error response from daemon: Conflict. The container name "/resq1-a" is already in use by container "66a85bfc…". You have to remove (or rename) that container to be able to reuse that name.

Run 'docker run --help' for more information
```

The load-bearing, matchable substrings are `Conflict.` and `is already in use by container`.

### Q1(b) Does `--rm` reliably remove the container when the daemon dies mid-run?

**UNKNOWN by direct measurement.** Killing/restarting the daemon requires
`systemctl restart docker`, which needs `sudo`; this host has no passwordless sudo and the attempt
was denied by the permission gate. I did not measure a true daemon death. Anyone who needs that
datum must run it by hand.

Two measured facts bracket the answer:

1. **`--rm` is daemon-side and survives the CLI's death (MEASURED).** `docker run --rm --name=X …
   sleep 25` was backgrounded, then the `docker run` client was `kill -9`'d at t≈3s. Observed:
   the container stayed `running` after the client died (and 6s later), and once it exited on its
   own the entry was gone — `docker ps -a --filter name=^X` returned `''`. So auto-removal is
   performed by the daemon on container exit, not by the client.
   This independently confirms the claim in `sandbox/container.py:13-17` that "killing the
   `docker run` client does not stop the container".

2. **A container that is REGISTERED but never STARTED is not reaped by `--rm`, and holds its name
   indefinitely (MEASURED).** This is the faithful proxy for the state a daemon death mid-`docker
   run` leaves behind:
   ```
   docker create --rm --name=resq1-e1 --network=none debian:bookworm-slim true   # exit 0
   state: 'resq1-e1 created'      # and still 'created' 4s later
   docker run --rm --name=resq1-e1 …  → exit 125, "Conflict. … already in use"
   ```
   `--rm` never fires because AutoRemove is triggered by container *exit*, and this container never
   ran. The name is held until something explicitly removes it.

**INFERRED (from 1 + 2):** the dangerous window is real and is exactly what `_invocation_name`'s
docstring (`buildverify.py:357-368`) describes — a daemon that registers a container and then dies
before it exits leaves a name that no retry can reclaim, and a fixed `--name` turns that into a
permanent 125. The measured `created`-state behaviour is the mechanism.

One nuance found while measuring (MEASURED): a container whose *start* fails for a config reason is
**not** left behind — `docker create --rm --workdir=/definitely/not/here …` then `docker start`
resulted in `docker ps -a` returning `''`. So not every failed start leaks a name; the leak needs
the daemon to stop participating.

### Q1(c) Is a name collision distinguishable from other 125s by exit code alone?

**MEASURED — no. Exit code alone is insufficient.** Every one of these returned **125**:

| cause | exit | distinguishing stderr |
|---|---|---|
| name collision | 125 | `Conflict. The container name "/X" is already in use by container "…"` |
| image absent locally and unpullable | 125 | `Unable to find image '…' locally` + `pull access denied for …, repository does not exist` |
| malformed `--memory` | 125 | `invalid argument "bogus" for "-m, --memory" flag: invalid size: 'bogus'` |
| malformed `--cpus` | 125 | (usage text; flag-parse failure) |
| nonexistent `--network` | 125 | `failed to set up container networking: network nope-net-xyz not found` |
| relative `--workdir` | 125 | `the working directory 'relpath' is invalid, it needs to be an absolute path` |

So **only the stderr text separates them.** Two contrasting codes were also measured, and both
matter:

- **command not found *inside* the image → exit 127**, not 125 (the container *did* start).
  Consistent with `_COMMAND_NOT_FOUND` handling in the harness.
- **unreachable daemon → exit 1, NOT 125** (MEASURED):
  ```
  DOCKER_HOST=unix:///nonexistent.sock docker run --rm … → exit 1
  stderr: failed to connect to the docker API at unix:///nonexistent.sock; check if the path is
          correct and if the daemon is running: dial unix /nonexistent.sock: connect: no such file…
  ```

**This last one contradicts the codebase.** `buildverify.py:844-846` tells operators, inside the
probe's own 125 message:

> "Other things `docker run` reports as 125: an unreachable daemon and an invalid flag or resource
> value…"

On Docker 29.7.2 an unreachable daemon reports **1**. The "invalid flag or resource value" half is
correct (MEASURED above). The unreachable-daemon half is wrong and should be corrected or dropped —
it is operator-facing prose that will misdirect a diagnosis. This is a documentation defect, not a
control-flow defect: nothing branches on it.

**INFERRED:** because an unreachable daemon yields 1 rather than 125, it does *not* reach
`_DOCKER_CANNOT_RUN`; it falls through to the generic non-zero path. Worth confirming against
`classify_build_failure`'s treatment of exit 1 before relying on it.

### Q1(d) Does `docker run --rm` racing `docker rm` produce its own failure mode?

**MEASURED — yes, and it is exit 137, which aliases onto "substantive failure".**

```
docker run --rm --name=resq1-r1 --network=none debian:bookworm-slim sleep 20 &   # backgrounded
sleep 3; docker rm -f resq1-r1     → prints "resq1-r1", exit 0
wait $RUNPID                       → docker run EXIT=137
```

The removal wins cleanly (exit 0) and the *run* dies with 137 (SIGKILL). So a container killed by a
sweep does not look like infrastructure trouble — it looks like a process that was SIGKILLed, which
a classifier keyed on 125 will not recognise as its own doing.

Also measured, and reassuring for the harness's teardown:

- `docker rm --force <absent>` → **exit 0, empty stderr** (idempotent).
- `docker rm <absent>` (no `--force`) → **exit 1**, `Error response from daemon: No such container: …`.

`ContainerSandbox.remove` (`sandbox/container.py:200-206`) uses `["rm", "--force", name]`, so its
docstring claim that "a container that is already gone is not an error" is **MEASURED-correct**.

*(Measurement-discipline note: my first pass at this took `$?` after a pipe into `tail`, which
reported `tail`'s status and made both look like exit 0. The numbers above are from a re-run that
reads the exit code directly off `docker`.)*

A short-lived `--rm` container racing a concurrent `docker rm -f` was run 8 times; all 8 removals
returned exit 0 with empty output (MEASURED). I did **not** observe a `No such container` race in
those 8 trials — but 8 trials is not evidence of absence, and `rm --force` is idempotent anyway, so
the race is benign in this direction. **UNKNOWN:** whether a tighter race window can make
`rm --force` fail; not reproduced.

### Q1.5 Critique of the incumbent proposal (not a ratification)

The token+prefix scheme is sound in its core and already shipped for `buildverify`. Four problems
remain, ordered by severity.

**(1) REAL DEFECT — the prefix sweep's regex over-matches on `.` in a repo id.**
`ContainerSandbox.list_by_prefix` (`sandbox/container.py:208-224`) builds
`docker ps --all --filter name=^{prefix}`. Docker's `name` filter is a **regex**, and `slug`
(`sandbox/worktree.py:33,39`) uses `_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")` — which
**preserves `.`**, a regex metacharacter (MEASURED: `slug("foo/bar.*baz$(x)")` → `foo-bar.-baz-x`).

Measured over-match:
```
containers: resq1d-a.b-1-tAAAAAAA   resq1d-aXb-1-tBBBBBBB
filter '^resq1d-a.b-1-t'  →  MATCH: resq1d-aXb-1-tBBBBBBB
                             MATCH: resq1d-a.b-1-tAAAAAAA
```
Both matched. So for a repo id containing `.` (common — `foo.js`, `my.repo`, `pkg.core`), a sweep
for repo `a.b` will also select and remove a *different* repo's container whose name differs only at
that position. Combined with Q1(d), the victim's `docker run` returns **137**, which is not
classified as infra — a cross-repo kill reported as a substantive failure. Fix: escape the prefix
(`re.escape`) before interpolating, or exclude `.` from `slug`'s allowed set.

**(2) The `-t` sentinel is correct and load-bearing — confirmed, keep it.**
`_container_prefix`'s docstring (`buildverify.py:346-349`) claims a bare `…-1` prefix would also
match attempt 10's containers. MEASURED and true:
```
filter '^resq1p-1-t'  →  resq1p-1-tabc123                        (only)
filter '^resq1p-1'    →  resq1p-10-tdef456  resq1p-1-tabc123      (both)
```
Do not "simplify" this away.

**(3) `spec_for_attempt`'s default is still the collision-prone name.**
`sandbox/container.py:114` defaults `name` to `sandbox_name(run_id, repo, attempt)`. Its docstring
(`:106-109`) does warn callers with re-issuing retry loops to pass their own. `buildverify` does.
Any future caller that forgets inherits the original bug silently. **Agent recommendation** (not a
directive): invert the default so the deterministic name must be requested explicitly.

**(4) `rdepverify.on_cancel` removes a container it never creates.**
`rdepverify.py:326-331` calls `sandbox.remove(sandbox_name(ctx.run_id, ctx.repo_id, ctx.attempt))`,
but that worker runs bazel **on the host** — it passes `cache.flag(sandboxed=False)`
(`rdepverify.py:283, 381`) and its own docstring at `:375-376` says it "has no `image`, builds no
container and emits no `--volume=`". MEASURED: it imports `ContainerSandbox` and `sandbox_name` and
nothing else container-shaped; there is no `docker_run_argv` or `spec_for_attempt` in the file.
Harmless today (`rm --force` on an absent name is exit 0, MEASURED) but it is dead code that
asserts a false model of the worker.

**Recommended scheme.** Keep `_invocation_name` + `_container_prefix`. Add: (i) `re.escape` on the
prefix at the `list_by_prefix` call, or drop `.` from `slug`; (ii) treat **137** as a sweep-induced
outcome where a sweep was issued, so a swept container is not billed as a substantive failure;
(iii) classify 125 by **stderr substring** (`Conflict.` / `is already in use`) rather than by code,
since the code is measured-ambiguous across six distinct causes; (iv) correct the
unreachable-daemon claim at `buildverify.py:845`.

---

## Q2 — Should a 125 on the *test* step replay the whole build?

**Answer: it currently does; it is wasteful but not unsound for a 125 specifically. The waste is
smaller than it looks, and the information needed to avoid it already exists and is discarded.**

### What actually replays (MEASURED, code read)

- Units come from `buildverify.py:798`: `units = [BUILD_UNIT] + ([TEST_UNIT] if payload.run_tests else [])`, recomputed unconditionally on every `run()`.
- `BuildverifyInput` has **no** `remaining_units`/`completed_units` field (contrast `symbolindex.py:170-176`, `classify.py:96`, `relocate.py:113`, `rewrite.py:211`), so nothing can tell the worker "build already passed".
- On failure the worker *does* report the split — `buildverify.py:875-883` returns `completed_units=["build"], remaining_units=["test"]` — but `BuildWorker._handoff` (`cli.py:5336-5368`) rebuilds units from its own `BUILD_UNITS` list and ignores the sub-result's `completed_units`. `verify` is atomic at that layer (`cli.py:4970-4992`).
- A checkpoint is written **only** for `status == "partial"` (`orchestrator/runner.py:509-510`). A 125 on the test step yields `status="failed"` (`buildverify.py:876`), so **no checkpoint is written**.
- `RETRY_TRANSIENT` does `continue` (`runner.py:546-552`) → checkpoint reloads as `None` → `BuildInput.remaining_units=None` → `BuildWorker.run` sets `owed = set(units)` (`cli.py:4947-4948`).

**Net (INFERRED from the above chain):** one transient test-step 125 replays buildgen re-render
(LLM-bearing on rungs ≥2, `buildgen.py:443-453`) + the probe `docker run` + the build `docker run` +
the test `docker run`, up to `max_transient_retries` (default 4, `retry.py:65,200-217`).

### Is the replay cheap? (MEASURED flags/mounts; INFERRED cost)

Persistent across containers — host bind mounts (MEASURED, `buildverify.py:235-245,283-303`;
`cli.py:7317-7323`; `settings.py:613-614`):
```
--volume=<root>/cache/bazel/disk:/cache/disk    --disk_cache=/cache/disk
--volume=<root>/cache/bazel/repo:/cache/repo    --repository_cache=/cache/repo
```
**Not** persistent — the output base. `docker/fleet-build.Dockerfile:114-116` sets
`startup --output_user_root=/home/fleet/.cache/bazel/_bazel_fleet`, and `/home/fleet` is inside the
image layer, mounted by nothing, with every container `--rm`.

**INFERRED:** every step already starts from a cold output base, so `bazel test` *already* redoes
the build's loading/analysis and re-materialises actions warm from `/cache/disk`. Replaying the
build therefore costs roughly **one extra warm build**, not a cold rebuild — real wall-clock, near-
zero recompute. Corroborated by `rdepverify.py:355-361`, which describes measuring "a rerun from a
fresh `--output_user_root` against that same repository cache". I did **not** time this myself:
the actual seconds are **UNKNOWN**.

### Is the tree still valid? (MEASURED + INFERRED)

The worktree is bind-mounted read-write at `/work` and is `cwd` for both steps.

- MEASURED: `--build_event_json_file=bazel-{unit}-events.json` (`buildverify.py:942`) is a
  **relative** path, so `bazel-build-events.json` / `bazel-test-events.json` land in the worktree root.
- MEASURED (documented in-tree at `cli.py:5075-5081`): `crate_universe`'s `crate.from_cargo`
  rewrites `//:Cargo.lock` in place.
- INFERRED: nothing sets `--symlink_prefix`/`--experimental_convenience_symlinks` anywhere in
  `src/`, so Bazel's default convenience symlinks (`bazel-out`, `bazel-bin`, …) are created in
  `/work` pointing into a path that does not exist on the host — dangling after each step. Nothing
  reads or cleans them. Whether they in fact appear is **UNKNOWN** (Bazel default, not pinned here).
- MEASURED: nothing cleans between steps or attempts. `git clean`/`reset --hard`
  (`vcs/git.py:474-486`) are used only at `cli.py:3758-3759` (Phase 2) and `vcs/commits.py:338-339`
  (rollback). The worktree is not re-cut per attempt: `orchestrator/context.py:184-186` is
  `work_dir / repo_id` with no attempt component, and `cli.py:7160-7166` cuts it once at planning.

**So the replay starts from a dirty tree.** The build step's own idempotency is what saves it, not
a reset.

### Soundness verdict per hazard (MEASURED unless noted)

| hazard | verdict |
|---|---|
| test step wrote something the build would now read | **No** — for 125 the container never started; that is the definition of `_DOCKER_CANNOT_RUN` (`buildverify.py:116-119`) |
| attempt counter burned | **No** on the transient path — `retry.py:205` bumps `transient_retries`, leaves `attempts`; pinned by `tests/test_workers_build.py:1190-1194` |
| an LLM repair patch already in the tree | **No** — `_diagnose` writes only two output fields (`:1048-1049`), touches no file |
| a git commit between build and test | **No** — `PUBLISH_UNIT` runs strictly after `VERIFY_UNIT` (`cli.py:4970-4999`) |
| replayed build reads different bytes | **Yes, potentially** — the `Cargo.lock` rewrite + events JSON. Publish is protected (planned bytes re-materialised, `cli.py:5086-5100`). "Replay the build" ≠ "re-run the identical experiment" |
| rung change alters the generated `BUILD.bazel` | **No** on the transient path (`runner.py:631`, ladder unchanged). **Yes** on `ADVANCE_LADDER`, which re-runs GENERATE (`buildgen.py:443-453`) |

**Conclusion.** For a test-step 125 the replay is **correct but wasteful** — sound because the
container never ran, wasteful because it re-pays a warm build and, on rungs ≥2, a buildgen LLM call.
It is **not** sound to generalise this to other test-step failures: the dirty-tree and
`ADVANCE_LADDER`-regenerates-BUILD.bazel rows above mean a replay is not always the same experiment.

Two secondary findings worth the orchestrator's attention:

- **The skip information exists and is thrown away twice** — `BuildverifyOutput.build_ok`
  (`:860`) and `WorkerResult.completed_units=["build"]` (`:878`) are dropped by
  `_handoff` (`cli.py:5348-5352`), and the result is never checkpointed because `failed ≠ partial`
  (`runner.py:509`). `BuildverifyInput` has no field to carry the answer back in.
- **The unbounded cost is the LLM one, not the CPU one** — `_diagnose` is called for *every*
  non-ok step including a 125 (`:873-874`), so past rung 1 each daemon blip buys a
  `BUILD_DIAGNOSIS` completion over docker's own stderr, which by the harness's own text
  (`:430-438`) "says nothing about" the repo. See Q3: that completion is then discarded.
- **MEASURED gap:** no test in `tests/` injects a 125 into the **test** step;
  `tests/test_workers_build.py:1071` only ever injects it into the build step.

---

## Q3 — `build_diagnosis` has no reader

### Q3(a) CONFIRMED — nothing consumes its output

**MEASURED.** The write sites are exactly two, and the read sites are zero.

Fields (`buildverify.py:574-580`):
```python
diagnosis: str = Field(default="", description="`build_diagnosis` prose, advisory only. The exit code is the verdict (§3.3).")
diagnosis_failure_class: FailureClass | None = Field(default=None, ...)
```
Writes (`buildverify.py:1048-1049`):
```python
output.diagnosis = response.value.root_cause
output.diagnosis_failure_class = response.value.failure_class
```
An exhaustive grep for `diagnosis` across `src/` yields only: those two declarations, those two
writes, the `roles.py:57` / `calls.py:357-361` / `schemas.py:151-162,304` plumbing, and prose. **No
`output.diagnosis` read, no `.diagnosis_failure_class` read, no `getattr`, no `"diagnosis"` string
key anywhere in `src/`.**

It is stronger than "written but never acted upon" — it is **not even persisted**. Three egress
paths were each checked (MEASURED):

1. **Parent handoff drops it.** `BuildWorker` (`cli.py:4983-4989`) and `VerifyWorker`
   (`cli.py:5480-5486`) copy a fixed field list — `steps`, `build_ok`, `test_ok`, `tests_ran` — and
   not the diagnosis. `BUILDVERIFY_STEP` appears nowhere that would make `BuildverifyOutput` a
   top-level dispatch result.
2. **Checkpoints don't carry it.** The only `checkpoints.save` in the tree
   (`orchestrator/runner.py:1011`) persists a `PhaseCheckpoint(completed_units, remaining_units,
   attempt)` (`:1005-1009`), not the worker output.
3. **`migration_state.json` doesn't carry it.** No `diagnosis` field in `state/projection.py` or
   `models/state.py`. The `ResultSink` path (`runner.py:492-498` → `cli.py:5885-5935`) persists
   fields off `BuildOutput`, which never received it.

**The only thing consumed is the cost, not the answer:** `response.usage` (`:1050`) → `usage`
(`:874`) → `WorkerResult.usage` (`:881`), billed by the budget machinery. So the harness pays
WORKHORSE-tier tokens on rungs 2–3 of every build failure and keeps only the token counter.

The docstrings at `:576` and `:1027-1029` frame this as intentional ("recorded beside, never over").
**INFERRED:** the code does not achieve even the "recorded" half — there is no durable record. The
only test touching it is the negative case, `tests/test_workers_build.py:2019`
(`assert out.diagnosis == ""`); nothing asserts it is ever populated.

The wire-up-vs-delete decision is the orchestrator's. The fact is: **no consumer exists.**

### Q3(b) CONFIRMED — the UUID in `log_path` destabilises the cache key

The mechanism verifies end to end. Every link is code I read (MEASURED):

1. **The evidence dict carries the path.** `buildverify.py:1033-1041`:
   ```python
   "log_path": None if result.stderr_path is None else str(result.stderr_path),
   ```
   This is the only `"log_path"` evidence key in `src/`.
2. **The path contains two per-run-unique components.** `util/proc.py:277-279,325`:
   ```python
   unique = f"{log_stem}-{uuid4().hex[:8]}"
   err_path = sink_dir / f"{unique}.stderr.log"
   ```
   and `sink_dir` is `Path(payload.log_dir) / str(ctx.run_id)` (`buildverify.py:908-913`). Shape:
   `artifacts/logs/<run_id UUID>/buildverify-<attempt>-<uuid4 hex[:8]>.stderr.log`.
3. **The whole evidence dict is rendered into the prompt.** `calls.py:266-278` JSON-dumps
   `evidence` into the user message body.
4. **The key is a hash of the rendered prompt.** `calls.py:281-289` hashes the message list;
   `cache.py:497-518` sets `prompt_sha256=prompt_sha256(messages)`; `cache.py:130-145` `compute()`
   folds `prompt_sha256` into the sha256 over all components.
5. **A miss under `read-only` raises.** `cache.py:469-472`:
   ```python
   if self._mode == "read-only":
       raise CacheMiss(role, key)
   ```
   `--llm-cache {read-write,read-only,off}` exists (`cli.py:356-361, 739-741`; `cache.py:85`;
   `settings.py:678`).

**Therefore (INFERRED, mechanically forced by the five measured links): every `build_diagnosis`
call computes a key never seen before, on every run, unconditionally.** Under `--llm-cache
read-only` the lookup misses. Under `read-write` the row is written and can never be hit again —
dead on arrival, growing the cache monotonically until `fleet gc --cache-max-age`
(`cache.py:36-39`).

**Aggravating detail worth flagging:** `CacheMiss` subclasses `LlmError` (`cache.py:88`), and the
call site catches bare `LlmError` (`buildverify.py:1044`). **INFERRED (not executed):** a
`read-only` replay therefore neither hits the cache nor fails loudly — it swallows the miss,
returns `TokenUsage()`, and leaves `diagnosis` empty. The "replay must not call a model" guarantee
degrades silently rather than surfacing. Given Q3(a), the swallowed value was never read anyway,
so today this is invisible; it would become a live bug the moment the field acquires a consumer.

**Secondary input, lower confidence:** `evidence["stderr_tail"]` is verbatim Bazel stderr, which
routinely embeds absolute sandbox paths and timings. Even with `log_path` removed the key would
likely still vary run to run. Whether any normalisation is applied to `stderr_tail`: **UNKNOWN** —
not traced.

**UNKNOWN:** whether any test pins a "read-only replay hits for build_diagnosis" property. Not
searched exhaustively, and `pytest` was not run.

---

## Q4 — `_c_toolchain_gate` clock branch

### The timeout the harness actually gives the probe

**MEASURED (code read).** `buildverify.py:189`:
```python
C_TOOLCHAIN_PROBE_TIMEOUT_S: Final = 120.0
```
Passed at `buildverify.py:784-788`:
```python
result = await runner(tuple(docker_run_argv(spec)), deadline=ctx.deadline,
                      timeout_s=C_TOOLCHAIN_PROBE_TIMEOUT_S)
```
And `util/proc.py:248-250` combines them by taking the earlier:
```python
if timeout_s is not None:
    candidate = loop.time() + timeout_s
    deadline = candidate if deadline is None else min(deadline, candidate)
```

**So the effective probe timeout is `min(ctx.deadline, now + 120s)`.** The constant's docstring
claim that it "can only TIGHTEN `ctx.deadline` … never extend it" is **MEASURED-correct**. The
docstring also states the sizing rationale explicitly: this is the attempt's first `docker run`, so
the 120 s is budgeted to cover the image pull, not `command -v`.

### What a >120 s pull currently produces — the Q4 claim is REFUTED

**MEASURED (code read).** The clock branch exists and fires first, at `buildverify.py:789-825`:
```python
clock = clock_failure(started=result.started, timed_out=result.timed_out)
if clock is not None:
    failure_class, retryable = clock
    ...
    return WorkerError(failure_class=failure_class, retryable=retryable, ...)
```
`clock_failure` (`workers/base.py:155-190`):
```python
if not started:  return FailureClass.TRANSIENT_INFRA, True
if timed_out:    return FailureClass.TIMEOUT, True
return None
```

A pull exceeding 120 s means the process **was** spawned and **was** killed at its deadline, i.e.
`started=True, timed_out=True` → `(FailureClass.TIMEOUT, retryable=True)`. The returned
`WorkerError` carries the `killed_at_deadline` prose (`:809-816`), which names the 120 s, says
"most likely mid-pull of an image this host has not fetched before", and states "The probe never
reached a verdict, so this is not evidence about the image's compiler either — it costs the repo a
rung, the same as any other timeout."

**So: a >120 s pull yields a retryable `TIMEOUT` that costs one ladder rung. It does NOT yield an
immediate permanent "install gcc" verdict.** The Q4 hypothesis is **refuted for the current code.**

**Why the claim was probably still in circulation:** the in-code comment at `:793-798` says this
branch was added to fix exactly the described bug —

> "Before this branch existed, BOTH shapes below fell through to the 'no C compiler' return: an
> immediate, PERMANENT, FABRICATED verdict about an image the probe never got to open…"

So the claim accurately describes a **pre-fix** state of this function. Anyone citing it against
current `main` is citing history. (INFERRED — I read the comment, not the git history; I did not
`git log` the fix, and the repo is not a git checkout at this path.)

### The three verdicts the gate can now return (MEASURED)

| condition | branch | failure_class | retryable |
|---|---|---|---|
| `payload.image is None` (host path) | `:768-769` | — (`None`, no gate) | — |
| never spawned (fleet deadline already past) | `:789-825` via `clock_failure` | `TRANSIENT_INFRA` | **True** |
| killed at 120 s / deadline (e.g. slow pull) | `:789-825` via `clock_failure` | `TIMEOUT` | **True** |
| `result.ok` | `:826-827` | — (gate passes) | — |
| exit 125 (`_DOCKER_CANNOT_RUN`) | `:828-850` | `BUILD_ERROR` | **False** |
| any other non-zero (the real "no compiler") | `:851+` | `BUILD_ERROR` | **False** |

The "install gcc"-shaped verdict at `:851` is reachable **only** when the probe actually executed
inside the image and exited non-zero with no clock failure — which is the correct precondition for
asserting anything about the image's compiler.

### One defect inside the 125 branch

The 125 message (`:836-849`) is otherwise good operator guidance — it names
`docker/fleet-build.Dockerfile`, explains that `settings.verify.container_image` is a LOCAL tag, and
explains why it is non-retryable. But its enumeration of other 125 causes includes "an unreachable
daemon", which **MEASURED is exit 1, not 125** on Docker 29.7.2 (see Q1(c)). Correct or drop that
clause. Nothing branches on it, so this is prose-only.

**INFERRED:** since an unreachable daemon returns 1, the probe's 125 branch will not catch it; it
falls through to `:851`, the "no C compiler in the sandbox image" verdict — **non-retryable
`BUILD_ERROR`**. That is the fabricated-verdict failure mode Q4 was worried about, still live, but
reached by a *different* door than a slow pull: a daemon that is down, not an image that is large.
Worth a targeted check before it bites; I did not execute the harness to confirm the path.

---

## Summary of defects found

| # | severity | where | claim |
|---|---|---|---|
| 1 | real | `sandbox/container.py:208-224` + `worktree.py:33,39` | prefix sweep regex over-matches on `.` in a repo id; can kill a sibling repo's live container (victim's run reports **137**, not infra). MEASURED |
| 2 | real (latent) | `buildverify.py:845` + `:851` | unreachable daemon = exit **1**, not 125 → misses the 125 branch, falls through to a fabricated "no C compiler" non-retryable verdict. MEASURED exit code; INFERRED fall-through |
| 3 | waste | `buildverify.py:873-874` + `:1048-1049` | `build_diagnosis` output has **zero** readers and is never persisted; only its token cost is consumed. Called even for a 125. MEASURED |
| 4 | waste | `runner.py:509` + `cli.py:5348-5352` | test-step 125 replays buildgen + probe + build + test; the `completed_units=["build"]` needed to avoid it is discarded twice. MEASURED |
| 5 | correctness (silent) | `cache.py:88` + `buildverify.py:1044` | `CacheMiss` is an `LlmError`, swallowed by a bare catch → `--llm-cache read-only` degrades silently instead of failing loud. INFERRED |
| 6 | hygiene | `container.py:114` | `spec_for_attempt` still *defaults* to the collision-prone deterministic name. MEASURED |
| 7 | dead code | `rdepverify.py:326-331` | `on_cancel` removes a container this worker never creates (it runs bazel on the host). MEASURED |

## Things I could not measure

- A true **daemon death mid-`docker run`** (needs `sudo systemctl restart docker`; denied, no
  passwordless sudo). Bracketed by two measured proxies — see Q1(b) — but not observed directly.
- **Wall-clock cost** of a warm build replay vs a cold one. Argued from mount/flag facts only.
- Whether Bazel's **convenience symlinks** actually appear in the worktree (no flag set either way).
- Whether any **test pins the read-only cache replay** property for `build_diagnosis`.
- Whether `stderr_tail` is **normalised** before entering the cache key.
- Git history for the `_c_toolchain_gate` clock branch (this path is not a git checkout).
