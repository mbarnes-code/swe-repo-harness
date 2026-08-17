# Task 11 report — Worker K

## Status: complete. D21 wired. `fleet` CLI works. Overrule of the `""` default upheld.

## Files created (`config/` did not exist at all)

- `config/fleet.yaml` — required: `settings.py:1128` reads `config/fleet.yaml` via
  `_read_yaml_mapping`, which raises `ConfigFileError` (`FileNotFoundError`, `:925-926`) if
  absent. All-defaults content (every `FleetConfig` field has a §9 default).
- `config/repos.yaml` — required, same mechanism (`:1129`). `version: 1`, `repos: []` (no repos
  enrolled yet; inventing fake ones would be fixture data, not config).
- `config/models.yaml` — required (`:1130`) and the one file defaults cannot satisfy:
  `ModelsConfig._default_profile_exists` (`:840`) fails when `profiles` is empty. `roles:` is
  `fleet.llm.roles.SPEC_ROLE_TIERS` transcribed verbatim (the harness's own "§9 roles as
  shipped" constant, `llm/roles.py:65`) — all 12 roles, not just the 2 the default ladder
  references, since `LlmRouter.__init__` (`llm/roles.py:118`) requires every `Role` by default
  and `fleet models list/check` construct one with no override. `profiles.default` gives each
  tier one priced `anthropic` target (§9 rule 5, `settings.py:1342` `UnpricedTargetError`).
- `config/rules/secrets.txt` — required to make `history_scrub_file`'s real default
  (`settings.py:310`) resolve instead of raising `HistoryScrubUnavailableError`
  (`vcs/filter_repo.py:181`). 9 `regex:...==>...` lines mirroring `RedactionSection.patterns`
  (`settings.py:294-305`) verbatim — real patterns, no secrets. Verified against git-filter-repo's
  actual parser (`FilteringOptions.get_replace_text`): **comment lines are not supported** in this
  file format (only `get_paths_from_file` skips `#`), so no comments were added — confirmed via
  `.venv/bin/python -c "... get_replace_text(...)"`, all 9 parsed as regexes, 0 as literals.
  Confirmed not caught by `.gitignore:44` (`config/secrets.*` matches only `config/`, not
  `config/rules/`) via `git check-ignore -v`.

## Overrule verified

The task-10 worker recommended defaulting `history_scrub_file` to `""`. I did not adopt that:
`.gitignore:44` scopes its secret-ignore to `config/secrets.*` (implying the rest of `config/` is
tracked), and 4 settings defaults point into `config/` (`:310`, `:447`, `:888`, `:1384`) — the
missing piece was the directory, not the default. Confirmed by provisioning it.

## `fleet` CLI before/after (measured via `fleet models list`, offline & side-effect-free)

- **Before** (`config/` absent): exit 2, `error: config/fleet.yaml: file not found`.
- **After**: exit 0, prints all 12 roles → tier → backend/model_id routes.

## D21 wiring

`src/fleet/cli.py:7116` (`_ingest_build_source`'s only `RelocationSpec(...)`): added
`replace_text=resolve_replace_text(settings.root, settings.config.redaction.history_scrub_file)`,
imported `resolve_replace_text` from `fleet.vcs.filter_repo`.

**Caught before landing:** wiring it raised `HistoryScrubUnavailableError` for the test suite's
own default-settings caller — `tests/test_transform_e2e.py`'s `_write_config` (shared by the
`fleet` fixture used in `test_build_e2e.py`) builds a synthetic `config/` with no
`rules/secrets.txt`. Verified via direct (non-pytest) reproduction, not by running the suite.
Fixed the fixture the same way as production: added one inert `secrets.txt` write to
`_write_config` (`tests/test_transform_e2e.py`, +11 lines). Neither file is in another worker's
lane; `tests/test_workers_build.py` doesn't touch `relocate`/`RelocationSpec` at all (checked,
0 hits) so it's unaffected.

## Test added

Extended `tests/test_build_e2e.py::test_build_reaches_phase_three_and_lands_the_generated_files`
(reuses the existing `filter_repo`/`FakeFilterRepo` argv-capture, rather than duplicating a full
Phase 1-3 e2e run) with 3 assertions: `--replace-text` is present in the captured argv, resolves
to `fleet/config/rules/secrets.txt`, and that file exists.

## Verification

`.venv/bin/ruff check src/ tests/` — all checks passed. `.venv/bin/mypy src/fleet/ --strict` —
success, 107 files. Did not run pytest (harness constraint).
