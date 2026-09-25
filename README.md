# Fleet Engine

**A migration harness that moves a fleet of polyglot repositories into a single Bazel monorepo.**

You point it at a list of source repos. It scans them, builds a cross-repo dependency graph,
topologically orders them into waves, rewrites and relocates each one into a monorepo worktree,
builds and tests the result under Bazel, and opens a stack of pull requests — resuming from where
it stopped if anything crashes.

Two design rules shape everything:

- **Deterministic work is done in code; a model is used only for judgment calls.** File moves,
  string rewrites, routing and retries are Python. An LLM is asked to classify, disambiguate or
  propose a repair, never to do bookkeeping.
- **Git is the sole source of truth for code**, tree SHAs, diffs and rollbacks. SQLite holds
  execution state only — task queues, step statuses, attempt counters, timestamps. There is no
  shadow version-control system.

> **Status: under construction.** Two commands (`fleet plan`, `fleet migrate`) are declared but
> not implemented and exit non-zero with a message naming the missing module. The rest run. The
> last checkpoint in [`docs/PROGRESS.md`](docs/PROGRESS.md) is authoritative on what is real — a
> green test run proves only what the tests actually cover.

---

## Requirements

| | |
|---|---|
| Python | `>=3.12` |
| OS | Linux — the memory guard reads `/proc/meminfo` and cgroup v2, and the sandbox shells out to POSIX tooling |
| Docker | Required for `fleet build` / `fleet verify` sandboxing — see [`docker/`](docker/) |
| Disk / RAM | The shipped `config/fleet.yaml` is tuned for a 48 GiB host. See [Configuration](#configuration) if yours is smaller. |

An LLM API key is only needed for the model-bearing steps. `fleet scan --skip-classify` and
`fleet transform --deterministic-only` run with no model at all.

## Install

The supported install uses the committed lockfile:

```bash
uv sync --frozen        # exact versions from uv.lock, including dev tooling
```

With plain pip, install the `dev` extra — not the bare package:

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

> `pip install -e .` without `[dev]` currently produces a `fleet` entry point that fails at
> import with `ModuleNotFoundError: No module named 'click'`. `src/fleet/cli.py` imports `click`
> directly, but `click` is not in `[project] dependencies` — it arrives only as a transitive of
> the dev toolchain. Use one of the two commands above until that dependency is declared.

Verify:

```bash
fleet --help
```

## Quickstart

```bash
# 1. Enroll the repos you want migrated and confirm model profiles.
$EDITOR config/repos.yaml
$EDITOR config/models.yaml

# 2. Check the LLM routing resolves and backend registration is valid (`list` is offline).
fleet models list
fleet models check

# 3. Create the state database. This is the only command that issues DDL.
fleet migrate-db

# 4. Phase 1 — scan every repo, then order the fleet into waves.
fleet scan
fleet sequence --emit plan.json

# 5. Phases 2–4, one wave at a time.
fleet transform --wave 1
fleet build     --wave 1
fleet verify    --wave 1
fleet pr        --wave 1

# At any point, from any shell:
fleet status
fleet status --watch --format json
```

If the run dies, `fleet resume` reconciles state and continues from each repo's re-entry floor —
not from the earliest incomplete phase. `fleet resume --dry-run` is a free health check that
writes nothing.

## Commands

Every command accepts the global flags `--config`, `--db`, `--run`, `--log-level`, `--json`,
`--llm-cache`, `--profile`, `--max-cost-usd` and `--max-rss-mb`. Run `fleet <command> --help`
for the full flag set of any one of them.

### Pipeline

| Command | Phase | What it does |
|---|---|---|
| `fleet migrate-db` | — | Bring the state database to this harness's schema version. The **only** DDL path. |
| `fleet scan` | 1 (1–5b) | Preflight, mirrors, manifests, coordinates, symbols, edges, contracts. |
| `fleet sequence` | 1 (6–8) | Build the DAG, break cycles, assign waves, audit collisions. Pure computation; refuses mid-flight (exit 11). |
| `fleet plan` | 2 | **Not implemented.** Would dry-run the relocation plan and rewrite target set. |
| `fleet transform` | 2 | Apply the relocation plan and the rewrite ladder; the commit is the record. |
| `fleet build` | 3 | `git-filter-repo` ingest + merge, BUILD generation, `bazel build` / `bazel test`. |
| `fleet verify` | 4 (1–3) | Re-test on tip, `bazel query rdeps`, test the closure, write a report. |
| `fleet pr` | 4 (4–5) | Ingest PR merge state, then open stacked PRs in topological order. |
| `fleet migrate` | 1–4 | **Not implemented.** Would run all four phases end to end. |

### Operating a run

| Command | What it does |
|---|---|
| `fleet status` | Read-only projection of SQLite. An output, not a service — nothing listens on a port. `--watch`, `--metrics`, `--digest`. |
| `fleet resume` | Reconcile after a crash and continue from each repo's re-entry floor. Refuses on config drift, wave cost ceiling or schema mismatch. |
| `fleet abort` | Stop deliberately, checkpoint, regenerate `migration_state.json`, exit 0. `--drain` (default) or `--now`. |
| `fleet retry <repo>` | Reopen one abandoned (`REQUIRES_HUMAN_INTERVENTION`) repo's phase to `PENDING`. `--reason` required. |
| `fleet quarantine <repo>` | Remove one pathological repo from the fleet mid-run without editing config. `--reason` required. |
| `fleet gc` | Retention only, never state. Refuses a run with live work unless `--force`. |

### Inspection

| Command | What it does |
|---|---|
| `fleet contracts list` / `inspect <id>` | The contract nodes found during scan: kind, owner, consumers, extractability, confidence. |
| `fleet stubs list` | Every stub, plus the "degraded and unresolved" reconciliation view. |
| `fleet stubs resolve <provider>` | Supersede a provider's active stubs and enqueue revalidation. |
| `fleet stubs abandon <consumer> <coord>` | Mark a stub abandoned by operator decision. `--reason` required. |
| `fleet models list` / `profiles` / `check` | Resolved `role → tier → backend` routing, available profiles, and backend registration checks. |

`--format json` is available on every table above. Most list commands accept repeatable
`--filter k=v`; `fleet models list` uses `--role` and `--tier` instead.

## Configuration

Fleet loads three YAML files on startup: `config/repos.yaml`, `config/fleet.yaml`, and
`config/models.yaml` (startup exits 2 if any is missing). `repos.yaml` and `fleet.yaml` can stay
minimal with defaults, but `config/models.yaml` must set `default_profile` to a profile that
exists in `profiles`.

| File | What it holds |
|---|---|
| `config/repos.yaml` | The fleet manifest. One entry per source repo: `name`, `url`, optional `ref`, `owns`, `dest`, `skip`. Ships empty. |
| `config/fleet.yaml` | The run's machinery: budgets, concurrency, and per-phase settings for scan, graph, transform, build, stubs, verify, llm, pr and gc. |
| `config/models.yaml` | LLM routing: each role maps to a capability tier, each profile maps a tier to an ordered list of backend targets. Three profiles ship: `default`, `local`, `pilot`. |

**Precedence:** CLI flags → `FLEET_*` environment variables → the YAML files → built-in defaults.

Nested keys use a double underscore, so `budgets.run_max_cost_usd` is
`FLEET_BUDGETS__RUN_MAX_COST_USD`. Note that any unrecognised `FLEET_*` variable is rejected
outright — the settings model forbids extra keys, so one stray export makes every command exit 2.

**API keys are read from the environment only, never from a config file.** A backend target names
the variable it wants (`api_key_env: ANTHROPIC_API_KEY`) and the backend reads it at call time.
Config text that matches a redaction pattern is refused at startup.

**If your host cannot satisfy 36864 MiB of commitment**, the shipped `config/fleet.yaml` can
refuse to load:
`concurrency.docker` × `verify.container_memory` + `budgets.max_rss_mb` must fit under
`budgets.max_host_rss_mb`. The shipped values are `4 × 8192 + 4096 = 36864` MiB. On a smaller
host, reduce the commitment inputs (`concurrency.docker`, `verify.container_memory`,
`budgets.max_rss_mb`), then set `budgets.max_host_rss_mb` at or above the resulting commitment.

## Exit codes

Non-zero exits include both specific refusals and one unexpected-failure path (`1`, listed below).

| | | | |
|---|---|---|---|
| `0` success | `1` unexpected error | `2` usage or config error | `3` run cost exceeded |
| `4` wave wall-clock timeout | `5` memory exceeded | `6` unresolved error collisions or manual cycles | `7` human intervention required |
| `8` no backend available for a required tier | `9` disk exhausted | `10` wave cost exceeded | `11` `sequence` refused mid-flight |

## Repository layout

```
src/fleet/        the executable package (asyncio, Pydantic v2, typer)
  cli.py            every command above
  workers/          the per-phase workers: scan, transform, build, verify
  graph/            the cross-repo DAG, cycle breaking, wave assignment
  llm/              ModelClient protocol + one file per backend
  state/            SQLite schema and migrations
  models/           Pydantic models and enums
  bazel/ ecosystems/ rewrite/ vcs/ sandbox/ manifests/ obs/ util/
tests/            pytest suites
config/           the three YAML files above
docker/           the two sandbox images (baseline: networked; build: hermetic)
tools/bin/        pinned toolchain wrappers — use these, never a system binary
tools/worktree/   per-agent git worktree provisioning
docs/             specification, decision log, and engineering archive
references/       READ-ONLY vendored source material
```

## Development

```bash
uv sync --frozen
.venv/bin/python -m pytest tests/ -q      # full suite is slow; run a file while iterating
.venv/bin/ruff check
.venv/bin/python -m mypy                  # no path arguments — pyproject sets the scope
tools/bin/security-scan                   # bandit + semgrep + pip-audit + gitleaks
```

Working on this repo with multiple agents in parallel? Each one gets its own git worktree —
see [`tools/worktree/README.md`](tools/worktree/README.md).

## Documentation

[`docs/README.md`](docs/README.md) is the reading guide. It separates the few documents worth
reading as a newcomer from the multi-megabyte engineering archive that records how the harness
was built.

The short version: read this file, then [`CLAUDE.md`](CLAUDE.md) if you are going to change code,
then the specific [`docs/SPEC.md`](docs/SPEC.md) section a task names. Do not read the archive
top to bottom.
