# Fleet Engine

A migration harness that moves a fleet of polyglot repositories into a single Bazel monorepo.

Deterministic work is done in code; a model is used only for judgment calls. Git is the sole source
of truth for code, tree SHAs, diffs and rollbacks; SQLite holds execution state only.

| Document | What it is |
|---|---|
| `docs/SPEC.md` | The specification. Section numbers (§1–§14) are the stable reference. |
| `docs/DECISIONS.md` | ADRs. Every non-obvious choice, with its rationale and rejected alternatives. |
| `docs/PROGRESS.md` | Checkpoint log. The last section is always the current state and the next task. |
| `CLAUDE.md` | Operating directives for agent-driven development of this repo. |

## Layout

```
src/fleet/        executable package (asyncio, Pydantic v2)
tests/            pytest suites
references/       READ-ONLY source material
```

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests/ -q
```

## Status

Under construction. `docs/PROGRESS.md` is authoritative on what is real and what is still a stub —
a green test run proves only what the tests actually cover.
