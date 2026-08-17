# Task 8 report (Worker H) — D21, D22

**Status: both measured OPEN at start; D22 now closed, D21 partially closed (render+resolver
wired and tested; the one production call site is outside my file ownership).**

## D21 — `--replace-text` secret scrub

Verified open before editing: `filter_repo.py:117/139-140` is the only render site and it never
fired because `cli.py:7116` (the sole `RelocationSpec(...)` construction in `src/`) never set
`replace_text`; `settings.history_scrub_file` (`settings.py:310`) had zero readers.

Changed (owned files only): added `resolve_replace_text(root, configured) -> Path | None` and
`HistoryScrubUnavailableError` to `src/fleet/vcs/filter_repo.py` — resolves the setting against
`FleetSettings.root` (mirrors `cli._forge_token_config`'s pattern), returns `None` for an
explicitly empty setting, raises loudly for a configured-but-missing file (never silently drops a
typo). Added tests to `tests/test_vcs.py`: `test_filter_repo_argv_renders_replace_text_when_set`,
`test_relocate_passes_replace_text_through_to_the_executed_argv` (fails if `--replace-text` stops
reaching the real argv), `test_resolve_replace_text_disables_the_scrub_on_an_empty_setting`,
`test_resolve_replace_text_resolves_an_existing_file_against_the_settings_root`,
`test_resolve_replace_text_refuses_a_configured_but_missing_file`.

**Not done, and why:** wiring `cli.py:7116` to call `resolve_replace_text` and pass the result as
`replace_text` is outside my ownership (`cli.py`), and — since `history_scrub_file` defaults to a
path that doesn't exist in this repo — making that call unconditionally would newly raise for
every existing caller that exercises the migrate pipeline with default settings, which I cannot
verify without running the full suite (barred). Recommend: `cli.py:7116` add
`replace_text=resolve_replace_text(settings.root, settings.config.redaction.history_scrub_file)`
to the existing `RelocationSpec(...)` call, verified by whoever owns `cli.py`/runs the suite.

## D22 — credential file mode

Verified open before editing: zero `chmod`/`st_mode`/`0o600` in `src/` (all 10 hits were in
`tests/`); `GiteaForge.__init__` accepted any `curl_config` path unchecked.

Changed: added `_require_private_credential_file()` to `src/fleet/vcs/gitea.py`, called from
`GiteaForge.__init__` right after `self._curl_config` is set. Stats the file; raises `GiteaError`
(loud, Rule 11) if group- or world-readable (`mode & 0o077`); does not check existence (that's
curl's own already-surfaced failure). Confirmed this host's real credential file
(`.secrets/gitea-curl.conf`) is `0600` — mode/content never read or printed. Added to
`tests/test_gitea.py`: `test_a_group_or_world_readable_credential_file_is_refused` (parametrized
0o644/0o640/0o604/0o666), `test_a_mode_600_credential_file_is_accepted`,
`test_a_missing_credential_file_is_not_refused_by_the_mode_check`.

## Verification

`.venv/bin/ruff check src/ tests/` — clean. `.venv/bin/mypy src/fleet/ --strict` — clean (107
files). Did not run pytest (barred by the reaper constraint).
