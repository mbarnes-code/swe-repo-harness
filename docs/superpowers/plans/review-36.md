# Review 36 — honesty audit of `44d5550..68a41ff`

**Scope.** Two commits: `44d5550` ("checkpoint 34: D34 fixed; clone worker misattributions;
ADR-0065..0067") and `68a41ff` ("checkpoint 35: clock_failure unifies the classifiers; D34 found
overstated"). Files touched: `src/fleet/vcs/git.py`, `src/fleet/workers/{base,buildverify,clone}.py`,
`tests/test_{vcs,workers_build,workers_scan}.py`, `docs/{DECISIONS,PROGRESS}.md`.

**Method.** `git show` / `git diff 44d5550~1..68a41ff`, plus reads of the pre-range state
(`44d5550~1`) to check historical claims, and of untouched files the range's claims depend on
(`orchestrator/retry.py`, `sandbox/container.py`, `rewrite/astgrep.py`, `cli.py`,
`docs/INTEGRATION_HONESTY.md`). No `pytest` was run, per the standing constraint.

**Headline verdict.** The range is **not** broadly honest, and the dishonesty is concentrated
rather than diffuse. `68a41ff`'s *commit body* is one of the most rigorous artefacts in this
repository — it retracts three separate overclaims in its predecessor and names the measurements
that forced each retraction. But **almost none of those retractions were carried into the files
they falsify.** The overstated sentences that `68a41ff` identifies are still sitting in
`buildverify.py`, in the operator-facing failure message, and — worst — in the *new* shared
docstring that `68a41ff` itself wrote as the canonical statement of the rule. The correction lives
in `git log` and nowhere a reader of the code will meet it. Separately, one ADR added in this range
describes a code change that does not exist, and the very next commit contradicts that ADR's
placement ruling.

**Counts: 3 Critical, 5 Important, 4 Minor.**

---

## Critical

### C1 — The operator-facing 125 message still asserts the claim `68a41ff` retracted

**Where.** `src/fleet/workers/buildverify.py:430-437` (`_DOCKER_CANNOT_RUN_EXPLAINED`), consumed at
`buildverify.py:464`.

**The claim.** The string ends:

> `"… Re-queued on the same rung as TRANSIENT_INFRA — no attempt charged, no repair prompted. docker stderr: "`

**The evidence against it.** `68a41ff`'s own commit body: *"'no attempt charged, no repair prompted'
is false past the 4-retry cap."* Confirmed in `src/fleet/orchestrator/retry.py`: the free-retry arm
is gated on `state.transient_retries < self.max_transient_retries` (`retry.py:200-202`) with
`DEFAULT_MAX_TRANSIENT_RETRIES = 4` (`retry.py:65`); past the cap control reaches the substantive
branch whose own comment reads *"A transient failure past its own cap lands here too — at that
point the endpoint's behaviour IS the evidence"*, which charges `attempts + 1`. And per
`PROGRESS.md` §35 B, `_diagnose` is gated on `ctx.context_policy is None` alone, so rungs 2–3 fire
two `BUILD_DIAGNOSIS` LLM calls for *any* class — "no repair prompted" is true only of the repair
rung, and only while the daemon returns.

**Why Critical rather than Important.** This is not a comment. It is prefixed onto `last_error`,
which is the text an operator reads during an incident. Every other item in this report misleads a
maintainer reading source; this one misleads the person deciding whether to page someone at 3 a.m.
It tells them the fleet absorbed the daemon outage for free. With `backoff_base_s = 0.5` and full
jitter, four transient retries are ~3.75 s of expected wall clock against a 5–15 s daemon restart,
so the *common* case is the one the message describes wrongly.

**Honest statement.** *"Re-queued on the same rung as TRANSIENT_INFRA, up to
`max_transient_retries` (4) times with jittered backoff — roughly 4 s of total delay. A daemon that
is still gone after that charges an ADR-0014 attempt and the ladder proceeds, including its
diagnosis rungs."*

---

### C2 — `clock_failure`'s docstring re-states the retracted claim as the canonical rule

**Where.** `src/fleet/workers/base.py:155-186`, specifically the bullet at `base.py:161-164`:

> *"ADR-0014 forbids charging a ladder rung for one: it is `TRANSIENT_INFRA`, which
> `RetryPolicy.decide` re-runs on the same rung with no attempt charged."*

**The evidence against it.** Identical to C1 — `retry.py:200-202` bounds it at four, then charges.
The statement is unconditional.

**Why Critical.** `68a41ff` exists to establish *one owner* for this rule ("**This function exists
so the answer is written down once**", `base.py:175`). Both `buildverify.classify_build_failure`
and `clone._error_for` now point here for their explanation. So the commit that retracted the
overclaim in its message simultaneously promoted that same overclaim to the single authoritative
docstring that two workers defer to. Every future reader who follows the pointer meets the false
version. This is the failure mode `docs/INTEGRATION_HONESTY.md`'s own header warns about, applied
to a docstring: *"converts 'we have not tested this' into 'we have tested this' without anybody
deciding to."*

**Honest statement.** *"…it is `TRANSIENT_INFRA`, which `RetryPolicy.decide` re-runs on the same
rung without charging an attempt for the first `max_transient_retries` occurrences; past that cap
the repeated failure is itself the evidence and a rung is charged."*

---

### C3 — ADR-0067 documents four code changes, none of which exist

**Where.** `docs/DECISIONS.md:4671` onward (added by `44d5550`).

**The claims.** ADR-0067's "Decision" section lists four concrete artefacts:

1. `ProbeIndeterminateError(RuntimeError)` in `rewrite/rules.py`, exported from `fleet.rewrite`;
2. a second `except` arm in `cli._transform_criterion`, ordered first;
3. `break` → `continue` in both arms, with a `(repo_id, engine)` dedupe;
4. `no_verdict(result: ProcResult) -> str | None` moved into `util/proc.py`, with
   `workers/clone.py`'s `_no_verdict` reduced to a thin delegator.

**The evidence against them, at `68a41ff`.**

- `grep -rn "ProbeIndeterminateError" src/ tests/` → **zero hits**.
- `grep -n "no_verdict" src/fleet/util/proc.py` → **zero hits**.
- `src/fleet/cli.py:4162-4164` is still a single `except EngineUnavailableError as exc:` followed
  by `unprobed.append(...)` and `break`.
- `src/fleet/workers/clone.py:199` is still the full `_no_verdict` implementation, not a delegator.

**Compounding it — an amendment written in the indicative.** `docs/DECISIONS.md:2222`, in the
"Amended by ADR-0067" block appended to ADR-0047 in the same commit, states:

> *"After ADR-0067 that is true only of a **genuinely absent binary**; a probe that ran and produced
> no verdict raises `ProbeIndeterminateError` and becomes a **violation**."*

There is no hedge and no "will". A reader of ADR-0047 is told the tree behaves in a way it does
not. Meanwhile `AstGrepRewriter._scan_for_error_nodes` (`rewrite/astgrep.py:181-206`) still raises
plain `EngineUnavailableError` for a killed or never-started probe, `_transform_criterion` still
buckets it into the non-blocking `parse_probe_unavailable` warning, and the run still exits 0 —
the exact laundered pass ADR-0067 was written to close.

**Note in mitigation, and its limit.** Recording a decision before implementing it is legitimate.
What is not legitimate is (a) the past-tense amendment at `DECISIONS.md:2222`, and (b) the absence
of any marker — no status line, no "not yet implemented", no `PROGRESS.md` entry — distinguishing
ADR-0067 from the ADRs around it that *are* shipped. ADR-0065 and ADR-0066, its immediate
neighbours, are both retrospective records of shipped state.

**Honest statement.** Add a status line to ADR-0067 (*"Decided 2026-08-16; NOT YET IMPLEMENTED as
of `68a41ff` — no `ProbeIndeterminateError` exists, `cli.py:4162` still `break`s on a single arm"*)
and put `DECISIONS.md:2222` in the future tense.

---

## Important

### I1 — `68a41ff` puts the shared decoder in the module ADR-0067 (committed one commit earlier) rules out, while claiming the answer is now written down once

**Where.** `src/fleet/workers/base.py:155` (`clock_failure`) vs `docs/DECISIONS.md:4757` (ADR-0067,
*"Where `no_verdict` lives, and why it is not `workers/clone.py`"*).

**The conflict.** ADR-0067 argues at length that the three-flag decoder belongs in `util/proc.py`
because *"the decoder belongs with the encoder"*, that `fleet.rewrite` importing from
`fleet.workers` *"inverts the layering"*, and — verbatim — *"A sibling worker landed the same
classifier as `clone._no_verdict` this round, with the right body and the right ordering. **It is
in the wrong module.**"* One commit later, `68a41ff` lands a third decoder of the same invariant in
`fleet/workers/base.py`. Neither commit body acknowledges the tension.

**And the "once" claim does not hold.** At `68a41ff` the `not started`-before-`timed_out` ordering
is written out by hand in **four** places:

| Site | Form |
| --- | --- |
| `workers/base.py:181-185` | `clock_failure` → `(FailureClass, bool)` |
| `workers/clone.py:218-220` | `_no_verdict` → reason string |
| `rewrite/astgrep.py:198` | `if result.started and not result.timed_out:` inline |
| `vcs/filter_repo.py:163`, `vcs/github.py:150`, `vcs/gitea.py:291` | `not result.started or exit == 127` |

**A partial defence, stated fairly.** `clock_failure` returns a `FailureClass`, which is a
`workers` concept and genuinely does not belong in `util/proc.py`; and `clone._no_verdict` now only
shapes the *message*, since the class comes from `clock_failure`. So the class decision really is
single-owner. That is a narrower and defensible claim than the one the docstring makes.

**Honest statement.** `base.py:175` should read *"This function exists so the FailureClass answer is
written down once. The reason-string decoder (`clone._no_verdict`) and the probe decoder
(`astgrep._scan_for_error_nodes`) still order the two flags by hand; ADR-0067's consolidation into
`util/proc.no_verdict` is still owed and would subsume all three."*

---

### I2 — The D34 test fixture pairs a stderr and an exit code that cannot occur together

**Where.** `tests/test_workers_build.py:1053-1069` (`DAEMON_GONE`, `_docker_cannot_run`) and
`tests/test_workers_build.py:1103` (`test_a_docker_daemon_that_went_away_is_not_reported_as_a_broken_build_file`).

**The claim.** `DAEMON_GONE`'s docstring: *"Verbatim `docker run` stderr when the daemon is
unreachable — the mid-wave restart case."* It is fed to `_docker_cannot_run`, which hardcodes
`exit_code=125`. The test name asserts the daemon-went-away case is handled.

**The evidence against it.** `68a41ff`'s commit body: *"MEASURED: `docker run` against an
unreachable daemon exits **1**, not 125, on Docker 29.7.2 — so D34's motivating case does not reach
the 125 branch at all and still buys the repair prompts it was meant to remove."* The fixture
therefore constructs a `ProcResult` docker does not emit: real stderr, counterfactual exit code.
Exit 1 falls through `classify_build_failure` to its last line and becomes retryable `BUILD_ERROR`
— precisely the defect D34 named.

**Why this survived.** `PROGRESS.md` §35 B item 1 does say *"No real Docker daemon was involved.
Every 125 in the suite is an injected `ProcResult`."* That is honest about the injection but not
about the pairing: it flags that the 125 was never observed, not that the daemon case was measured
to produce something else. And it was written before the measurement.

**Honest statement.** Rename to `test_a_docker_run_that_exits_125_is_not_reported_as_a_broken_build_file`,
change `DAEMON_GONE`'s docstring to name a case that really does exit 125 (per the commit body: a
surviving container colliding with `--name=`), and add a `# NOT the daemon-unreachable case — that
exits 1; see D34 correction in 68a41ff` marker. The daemon-restart case needs its own test and its
own branch, and it does not currently have either.

---

### I3 — A newly measured, unfixed defect is recorded only in a commit body

**Where.** `68a41ff` commit body; nothing in `docs/INTEGRATION_HONESTY.md` (untouched by the range;
`D45` at line 2068 remains the maximum) and nothing in `docs/PROGRESS.md`.

**The finding it records.**

> *"MEASURED: a surviving container makes `docker run --name=X` exit 125, and RETRY_TRANSIENT
> re-runs the same rung so the name is byte-identical. A dead daemon leaves the container (`--rm`
> is the daemon's job), so the harness's own recovery manufactures a permanent 125 that consumes
> all four free retries."*

**Corroborated against the tree.** `sandbox/container.py:104` derives the build container's name
from `(run_id, repo, attempt)`; `docker_run_argv` emits `--rm` and `--name={spec.name}`
(`container.py:122-123`). `RetryAction.RETRY_TRANSIENT` re-runs the same rung without incrementing
`attempts` (`retry.py:203-217`), so `ctx.attempt` — and therefore the name — is unchanged across
all four free retries. A container the dead daemon never reaped makes every one of those four
retries fail identically.

**Severity of the underlying defect.** This is worse than the D34 it emerged from. D34 wasted three
rungs on a *transient* condition. This one is self-inflicted and *deterministic*: the fix for D34
guarantees four byte-identical retries, each of which is guaranteed to fail, before charging the
ladder — and the container that causes it is the previous attempt's own debris. It has no number,
no ledger entry, and no test.

**Honest statement.** It needs a `D46` entry in `docs/INTEGRATION_HONESTY.md` at its measured
severity, and it wants a name that varies with `transient_retries`, not just `attempt`.

---

### I4 — The unification's account of its own history is wrong: `44d5550` created the divergence it blames on the past

The brief asked specifically whether the two branches the comment claimed were "in step" actually
were. **They were.** The divergence was manufactured inside this range.

**Where the false history appears.** `src/fleet/workers/base.py:176-179`; repeated at
`src/fleet/workers/clone.py:700-706`; repeated again in the new test's docstring at
`tests/test_workers_scan.py:729`ff; and asserted in `68a41ff`'s commit body (*"a comment that
CLAIMED they were in step while they were not"*).

**The claim.** `base.py:176-179`:

> *"`buildverify.classify_build_failure` and `clone._error_for` **used to each draw this line
> themselves**, with a comment in one asserting the two were 'meant to stay in step' — and **they
> were not**: clone discarded `started` on the way into its `GitCommandError` and answered `TIMEOUT`
> for both."*

**The evidence.** At `44d5550~1`:

- `buildverify.classify_build_failure` read `if result.timed_out: return TIMEOUT` **first**, then
  `if not result.started: return TRANSIENT_INFRA`.
- `clone._error_for` read `failure_class = TIMEOUT if timed_out else TRANSIENT_INFRA`.

For the never-started `ProcResult` (`started=False`, `timed_out=True`, `exit_code=124`) — the only
shape at issue — **both answered `TIMEOUT`**. They were in step. Wrong, but in step.

- The `"the two are meant to stay in step"` comment does not exist at `44d5550~1`. It was
  **introduced by `44d5550` itself**, at `44d5550:392`, in the same hunk that reordered
  buildverify's two branches and left `clone._error_for` untouched.

So the true sequence is: `44d5550` fixed one of two matched call sites, did not fix the other, and
wrote a comment asserting they matched. The comment was false the moment it was committed —
falsified by its own commit. `68a41ff` correctly identifies that the comment was false, but
misattributes the cause to clone's long-standing `GitCommandError` construction rather than to
`44d5550`'s own half-applied reorder. Two further statements inherit the error:

- *"used to each draw this line themselves"* — `clone._error_for` never drew this line. It had no
  `started` branch, ever.
- `tests/test_workers_scan.py:729`ff and `clone.py:705`: *"while the build worker, for the identical
  `ProcResult`, answered free `TRANSIENT_INFRA` and charged nothing"* — true only of the ~90 minutes
  between `44d5550` and `68a41ff`, not of any code that ran a real wave.

**Why this matters beyond pedantry.** The stated history sells "a shared callee makes agreement
mechanical rather than aspirational" as the lesson. The actual history teaches a sharper one: *a
single-call-site fix to a two-call-site invariant, shipped with a comment asserting the other site
agrees, is how you get a regression that no test can see.* `PROGRESS.md` §35 D says the same thing
correctly — *"Found by a code review of §34's own clone fixes"* — so the accurate account exists in
this range; it just did not reach the code.

**Honest statement.** *"`44d5550` reordered these two branches here and did not reorder
`clone._error_for`, then asserted in a comment that the two agreed. They had agreed before that
commit — both answered `TIMEOUT` — and stopped agreeing because of it."*

---

### I5 — The `started` fix is incomplete: one `GitCommandError` construction site still drops the flag, under a docstring that implies none do

**Where.** `src/fleet/cli.py:3629-3640`, against `src/fleet/vcs/git.py:106-111`.

**The claim.** `68a41ff`'s commit body: *"`GitCommandError` gained `started`, which fixes the same
defect one layer down."* The new docstring at `git.py:110`: *"It defaults to `True` because every
other raise site here is a git process that demonstrably ran and exited."*

**The evidence.** There are three construction sites of `GitCommandError` in `src/`:

| Site | Forwards `started`? |
| --- | --- |
| `vcs/git.py:252` | yes (added by `68a41ff`) |
| `workers/clone.py:241` | yes (added by `68a41ff`) |
| `cli.py:3634` | **no** |

`cli.py:3634` raises on `if not result.ok or result.stdout_path is None:` from a direct
`proc_run(...)` — a condition that plainly includes `started=False` — and forwards
`timed_out=result.timed_out` while omitting `started`. It therefore defaults to `True`, and the
exception renders as `"timed out"` for a command never spawned. The word *"here"* in the docstring
scopes the claim to `vcs/git.py`, so it is technically true; but it invites the reading that the
default is safe everywhere, and one site out of three falsifies that.

**Honest statement.** Either forward `started=result.started` at `cli.py:3634`, or amend
`git.py:110` to *"It defaults to `True` for hand-constructed raises; every site that builds one from
a `ProcResult` must forward the flag, and `cli.py:3634` currently does not."*

---

## Minor

### M1 — `test_a_daemon_blip_costs_the_repo_no_attempt_and_reaches_no_human` generalises past what it drives

**Where.** `tests/test_workers_build.py:1169`.

The body drives `_docker_refuses_the_build_step(blips=1)` — one 125, then green. The name says
"a daemon blip", and the assertion message says *"a daemon that restarted mid-wave may not consume
one of this repo's three ADR-0014 chances"*, which holds only for ≤ 4 blips. Nothing exercises
`blips=5`, which is where `retry.py:200-202` stops being free — the exact boundary `68a41ff`
retracted D34 over. The docstring's own sentence is precise (*"One 125 followed by a healthy daemon
is a repo that SUCCEEDS having spent nothing"*); the name and the assertion message are not.

**Honest statement.** Rename to `test_a_single_daemon_blip_costs_the_repo_no_attempt`, and add the
missing sibling at `blips=5` asserting `attempts == 1` — that test is what would have prevented C1
and C2 from being written.

---

### M2 — The cross-module agreement test pins `retryable` vacuously on the clone side

**Where.** `tests/test_workers_scan.py:729`ff, the assertion
`assert (cloned.failure_class, cloned.retryable) == built == expected`.

`clone._error_for` builds its `WorkerError` with a **hardcoded** `retryable=True`
(`clone.py:716`), and discards `clock_failure`'s second element outright:
`failure_class, _ = clock_failure(...)` at `clone.py:710`. So the `retryable` half of that tuple
equality cannot fail on the clone side by construction — the test would still pass if
`clock_failure` returned `retryable=False` for `TIMEOUT`. The test genuinely pins the class; it
only appears to pin retryability.

**Honest statement.** Either have `_error_for` use the returned retryable
(`failure_class, retryable = clock_failure(...) or (FailureClass.TRANSIENT_INFRA, True)`), which
makes the assertion mean what it says, or narrow the assertion and note that clone's retryability
is a constant.

---

### M3 — `68a41ff`'s commit body says the new test "pins the boundary"; the test says it does not

**Where.** `68a41ff` body — *"and pins the boundary, since for a process that FINISHED the two
should differ (buildverify reads Bazel's exit table, clone has none)"* — against
`tests/test_workers_scan.py:729`ff.

The test's own docstring is scrupulous: *"What is asserted for that row is only that neither
invents a clock failure."* And the code matches — the finished row asserts only
`is not FailureClass.TIMEOUT` on each side. Nothing asserts that they *differ*, and `exit_code` is
never varied for that row. This one is unusual in the range: **the test is more honest than the
commit message describing it.** Worth recording only because it is the same overclaiming reflex
that produced C1 and C2, caught here at low stakes.

---

### M4 — `_is_shallow`'s docstring overstates its measurement base and contradicts itself within four sentences

**Where.** `src/fleet/workers/clone.py:723-745`.

Two problems.

1. *"**Measured across git 2.20.4 → 2.49.1**: a successful `fetch --unshallow` always REMOVES the
   file."* `docs/INTEGRATION_HONESTY.md`'s environment header records this host's git as **2.43.0**,
   and nothing in the repo records a multi-version sweep — no matrix, no fixture, no artefact. A
   claim spanning fifteen git releases needs a pointer to where it was taken.
2. *"**Reading the file is the right signal, and it is the same one git reads.**"* The next sentence
   says `git rev-parse --is-shallow-repository` answers `true` where `_is_shallow` answers `False`,
   and `tests/test_workers_scan.py:384`ff asserts exactly that divergence against the real binary.
   They are demonstrably **not** the same signal — that is the entire point of the change.

Also unmeasured: the test's parenthetical at `test_workers_scan.py:~405` — *"(A file holding blank
or whitespace lines is instead rejected by git with `fatal: bad shallow line`…)"* — is asserted in a
comment while the test invokes git only for the zero-byte case.

**Honest statement.** *"Reading the file is a **narrower** signal than `git rev-parse
--is-shallow-repository`, deliberately: git's predicate answers `true` for a zero-byte `shallow`
on a complete repo (measured, and pinned by the test below), and this does not."* Plus a pointer to
where the version sweep was run, or drop the range to the version actually measured.

---

## What holds up

Recorded because a clean result is a result, and because these were the specific things checked:

- **The `clock_failure` unification preserves both call sites' behaviour.** `buildverify`'s
  post-`44d5550` mapping is reproduced exactly. `clone._error_for`'s two pre-existing outcomes
  (`started=True, timed_out=True` → `TIMEOUT`; `started=True, timed_out=False` → `TRANSIENT_INFRA`)
  are both preserved, and the third (`started=False` → `TRANSIENT_INFRA`) is the intended fix. The
  `or (TRANSIENT_INFRA, True)` fallback correctly handles `OSError`, which carries neither
  attribute. No behavioural regression found. (See M2 for the one cosmetic wart.)
- **The branch ordering is correct and load-bearing**, and the reasoning given for it
  (`base.py:170-173`) survives contact with `util/proc.py`'s three-flag synthesis.
- **The shallow-gate comment correction is sound.** The claim it removed (git-filter-repo refuses a
  shallow repo) is indeed absent from upstream, `relocate()` does pass `--force`, and the
  replacement hazard — a rewrite that succeeds and imports truncated history — is the more
  dangerous and more accurate framing. The gate text was updated to match.
- **`PROGRESS.md` §35 B ("What D34 did NOT fix")** is the most honest passage in the range: it
  volunteers that no real daemon was involved, that the Bazel-125 premise rested on absence of
  evidence, and that the probe-before-build ordering is a property of the call graph rather than of
  the function. It is stale only in the *safe* direction (item 2 is now superseded by the jar
  enumeration in `68a41ff`'s body).
- **ADR-0047's measured-correction blockquote** is scrupulous: it records a proposed correction that
  was **refuted** by measurement, and explains why the entry stands. That is the behaviour this
  audit is meant to reward.
- **`ADR-0065` and `ADR-0066` were not evaluated** — both predate the range (present at
  `44d5550~1`, unmodified by either commit), despite `44d5550`'s `ADR:` trailer listing all three.
  That trailer overstates by two; folded into the observations below rather than counted, since it
  is a metadata slip with no effect on the tree.

## Documentation state, for the orchestrator

Not counted as findings — these are follow-ups the range leaves open, and each is the *mechanism*
by which the findings above stayed uncorrected:

1. **No `PROGRESS.md` §36.** The file ends at §35 (`PROGRESS.md:4129`), which was added by
   `68a41ff` but documents `44d5550`'s work. `68a41ff`'s own four code changes — `clock_failure`,
   `GitCommandError.started`, `_is_shallow`, the shallow-gate comment — and its three D34
   retractions have **no checkpoint entry at all** (Rule 10). Worse, §35 D presents the retry-rung
   divergence as *"Newly recorded, not yet fixed"* while the commit that added those words fixes it,
   and the §35 headline still reads *"D34 is fixed"* with none of the qualifications from the body
   of the same commit. **This is the single mechanism most responsible for C1 and C2:** the
   retractions had no destination, so they stayed in the commit body.
2. **`docs/INTEGRATION_HONESTY.md` untouched by the range.** D34 (line 1896), D35 (1914), D36
   (1928) and D41 (2004) all still read `— OPEN` though all four are fixed and tested at `68a41ff`.
   §35's own "Loose ends" flags this, so it is known rather than hidden.
3. **Docker version drift.** The ledger's environment header records `docker` server **29.6.2**;
   every measurement in `68a41ff` is on **29.7.2**. The 125-vs-1 finding — the load-bearing fact of
   the whole correction — is version-specific, and nothing in the repo records the upgrade or
   re-runs the header's other docker rows against it.
