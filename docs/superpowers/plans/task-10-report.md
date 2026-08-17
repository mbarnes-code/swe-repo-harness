# Task 10 report — Worker J

**Status:** both tasks complete, verified.

## Task 1 — `GitCommandError.started` required keyword

Made `started` a required keyword-only param (removed `= True` default) in
`src/fleet/vcs/git.py::GitCommandError.__init__`. Searched all of `src/` and `tests/` for
`GitCommandError(` construction calls (excluding the class definition itself): found exactly 3
sites — `vcs/git.py:258`, `workers/clone.py:241`, `cli.py:3634`. All 3 already forwarded
`started=result.started` explicitly (a prior worker had already fixed the one regressed site,
`cli.py:3634`, per the I5 note and the AST-based regression test at
`tests/test_cli.py:1315` `test_every_gitcommanderror_construction_forwards_started`). No test
directly constructs `GitCommandError`. Result: zero call-site edits needed beyond the signature
itself; updated the class docstring to explain why the field is now required instead of defaulted.

## Task 2 — Pyright vs mypy on `LayoutAdapter.monorepo_dir`

**Verdict: the protocol declaration was wrong; fixed it.** Every real `EcosystemAdapter`
(`ecosystems/{jvm,py,rust,go,js,unknown}.py`) declares `monorepo_dir: ClassVar[str] = "..."` — a
plain class constant, never computed. `bazel/layout.py`'s `LayoutAdapter` Protocol declared it via
`@property` instead, which is structurally looser than the real contract (`path_tail` is the
member that's genuinely computed per-call).

Evidence: built isolated repros under a real venv-independent tmp package (not this project's
imports, so pyright's known venv-resolution weakness doesn't apply here — confirmed by testing
mypy strict on the same repros, which passed in every case). Pyright errors specifically when the
class being protocol-checked *directly* declares the member as `ClassVar` (with or without a
default) against a Protocol `@property`; it does NOT error when a subclass merely inherits and
reassigns without re-annotating. `EcosystemAdapter` (the ABC returned by `for_ecosystem`'s
declared return type) hits exactly the failing shape. mypy accepted all variants, consistent with
PEP 544: a read-only (getter-only) property protocol member is satisfiable by any attribute kind.
Changed `LayoutAdapter.monorepo_dir` from `@property` to `ClassVar[str]` — this matches the real
shape used by every implementer, and after the fix `pyright src/fleet/bazel/layout.py` no longer
reports the "not defined as a ClassVar in protocol" error (confirmed against pre-change baseline
via `git stash`, which still showed it). One unrelated, pre-existing pyright complaint remains on
`path_tail`'s docstring-only body (present before and after this change, not part of the reported
disagreement, out of scope).

## Task 11 — D21 wiring: NOT applied, blocking bug found

**Do not wire `cli.py:7116` yet.** `resolve_replace_text`'s "not configured" branch fires only on
an *empty/blank* string. But `settings.py:310` defaults `history_scrub_file` to the non-empty
`"config/rules/secrets.txt"` — and `config/` does not exist in this repo (confirmed:
`ls config/` → No such file or directory). So under true default settings — no user opt-in at all
— `resolve_replace_text(settings.root, settings.config.redaction.history_scrub_file)` resolves a
non-empty `cleaned`, finds the file missing, and raises `HistoryScrubUnavailableError` for every
default-settings caller, this repo included. The function's emptiness check is correct in
isolation; the settings default just never produces the empty state that means "not configured."
No existing test in `test_vcs.py` exercises the actual default value. Recommend `settings.py`'s
`history_scrub_file` default become `""` (opt-in scrub) so "not configured" and "default" coincide;
`resolve_replace_text` needs no change. Did not touch `settings.py`, `filter_repo.py`, or
`cli.py:7116` — those are outside my lane and the settings default is the actual bug.

## Files changed
- `src/fleet/vcs/git.py`
- `src/fleet/bazel/layout.py` (outside originally-listed lane; required to fix the exact
  diagnostic Task 2 named, which roots in the Protocol declaration, not `cli.py` itself; no other
  worker was named as owning it)

## Tests
No new tests added (existing `tests/test_cli.py::test_every_gitcommanderror_construction_forwards_started`
already guards Task 1; no test file exists for `bazel/layout.py`'s Protocol shape).

## Verification
`.venv/bin/ruff check src/ tests/` — clean on all owned files (one pre-existing unrelated
`RUF043` finding in `tests/test_vcs.py`, owned by another worker, not touched).
`.venv/bin/mypy src/fleet/ --strict` — `Success: no issues found in 107 source files`.
`~/.local/bin/pyright src/fleet/bazel/layout.py` — target error gone (confirmed absent via
`grep -c "ClassVar in protocol"` = 0; present pre-change via `git stash`).
