"""`ruff check` is a GATE, and the pin is what makes gating it safe.

**Why this file exists.** `ruff check` was red on `main` — 8 errors in one test file, surviving
across two refs — while a round-F report called it "clean". That report was true of the run it
made and false of the repository: the run was scoped to a file and the scoping was not
disclosed. Nothing in the suite would have caught it, because nothing ran `ruff` at all. So the
first three checks here run `ruff` over the whole repository and put the resolved scope in the
failure message, where a reader cannot mistake it for something wider.

**Why the pin is part of the same change, and why a gate alone would be a defect.**
`[tool.ruff.lint].select` lists *family prefixes* — `E`, `B`, `SIM`, `RUF`, `S`, `ANN`, `TRY`.
A family prefix selects rules that do not exist yet: every ruff release that adds a rule to one
of those families is enabled here the moment somebody installs it. Gating an unpinned linter
therefore hands an upstream release the power to redden a tree nobody touched, and the failure
arrives on whichever unlucky lane next builds an environment. `pyproject.toml` pins
`ruff==0.16.2` in both dependency tables, and `test_the_ruff_that_will_run_is_the_ruff_that_is
_pinned` checks the *resolved* binary rather than the declaration, because a declaration read
is not a value exercised.

**On absence.** `ruff` missing does not skip. It is a declared `dev` dependency, so a `ruff`
that is genuinely absent is a broken environment, not weather — and a gate that no-ops when
its tool is missing is a convention wearing a mechanism's clothes. `_ruff()` fails naming
every directory it searched. This follows `_fail_if_registry_unreachable` in
`tests/test_bazel.py`, which was rewritten from a skip for exactly this reason.

**And "absent" is not the same question as "absent from `REPO_ROOT/.venv/bin`".**
`tests/conftest.py` prepends that directory to PATH at import time, which answers *where would
this checkout's virtualenv be*, never *which environment is running*. The two are one directory
in the primary checkout and two in a detached worktree, which has no `.venv` of its own — and
every lane in this project works in a detached worktree, because `BAZEL_ROOT` is keyed on
`sha256(REPO_ROOT)` and concurrent pytest sessions in one checkout reap each other's Bazel
output base. At `f6a2e4e` that made three of these four checks fail in a clean worktree with
`ruff` installed and runnable, reported by lane W1 and reproduced with no patch applied; see
`_ruff()` for what searching the running interpreter's own `bin/` does and does not change.

**What is deliberately NOT gated clean:** `ruff format --check`. Measured whole-repo at
`12d3527`, 117 of 142 files were dirty under it; gating it *clean* would redden `main` on the
same commit that added the gate, and reformatting those files is explicitly out of scope for
this criterion (`docs/CRITERIA_PLAN.md` §2's done bar — a separate, larger, disruptive change).
Re-measured at `b9524af` (round R, task 3): 123 of 282 files are dirty — both numbers moved
since `12d3527` in the ordinary course of the repository growing, and `ruff format` in the
version now pinned also formats Python code fences embedded in `.md` files, which is why `docs/`
paths appear in the dirty set. `test_ruff_format_check_dirty_count_matches_the_pinned_baseline`
below turns that measurement into a *baseline* pin, not a clean gate: it fails on drift in
EITHER direction, so a handful of files silently reformatted (or newly gone dirty) is caught
even though the whole set staying dirty is not itself a failure. Reformatting down to 0 remains
out of scope; when it happens, the pin drops to 0 in the same commit, deliberately.

**Note, added at `54b2c80` (round R final-fix):** the `282` denominator above went stale on the
very commit that landed it — `5f8fa73` (round R's own plan-file commit) added one new,
format-clean `.md` file to the tree. Re-measured at `54b2c80`: **123 dirty, 160 clean, 283
scanned.** The `123` dirty count and the pinned assertion below are unaffected; only the clean
count and the total moved. Per this project's annotate-in-place convention, the `b9524af` figure
above is left as written and this note states the current one beside it rather than overwriting.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
import warnings
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"

# The trees this project owns. `references/` is READ-ONLY by directive (CLAUDE.md §5) and holds
# 1660 `.py` files at `12d3527`; `work/`, `cache/`, `mirrors/` and `artifacts/` are runtime
# write paths that fill with *other repositories'* source. Today ruff excludes all of them, but
# it excludes them by honouring `.gitignore` — a default (`respect-gitignore`) and a file that
# neither this gate nor `[tool.ruff]` controls. `test_ruff_resolves_only_this_projects_own_files`
# turns that implicit exclusion into a loud one instead of duplicating `.gitignore` here, which
# would be a second source of truth for the same list.
_OWNED_TREES = ("src", "tests")

_RUFF_REQUIREMENT = re.compile(r'^\s*"(ruff(?:\[[^\]]*\])?)([^"]*)"\s*,?\s*$', re.MULTILINE)


def _ruff() -> str:
    """The absolute path of the `ruff` that will run, or a failure that names where we looked.

    Two places, in this order: PATH exactly as `tests/conftest.py` left it, then the `bin/` of
    the interpreter actually running this suite. The second is searched only after the first
    finds nothing, so wherever the PATH-only predicate resolved a binary this one resolves the
    same file — the fallback is additive, and cannot change which `ruff` an environment that
    already has one runs. What it does change is the detached-worktree case, where PATH carries
    a `REPO_ROOT/.venv/bin` that does not exist while `ruff` sits beside `sys.executable`.

    Absence still fails: if nowhere has it, this fails naming everywhere it looked.
    `sys.executable` is the environment that will run the code (Guardrail 6), which is why it
    is the right second question to ask — a checkout-relative path is a declaration, not a
    resolved value.

    Do NOT `.resolve()` that path. `.venv/bin/python` is a symlink to the system interpreter,
    so `Path(sys.executable).resolve().parent` is `/usr/bin` and the fallback silently looks in
    the wrong directory — measured, in the first draft of this fix, which reproduced the exact
    3-of-4 failure it was written to remove. `sysconfig.get_path("scripts")` is asked first
    because it answers the question directly ("where does THIS environment install console
    scripts", and `ruff` is one); the unresolved parent of `sys.executable` is a second,
    independent derivation of the same directory.
    """
    candidates = [sysconfig.get_path("scripts"), str(Path(sys.executable).parent)]
    found = shutil.which("ruff")
    for candidate in candidates:
        if found is None:
            found = shutil.which("ruff", path=candidate)
    if found is None:
        interpreter_dirs = "\n  ".join(candidates)
        path_dirs = "\n  ".join(os.get_exec_path())
        pytest.fail(
            "`ruff` is nowhere this gate can find it, so the lint gate cannot run — and a lint "
            "gate that passes when its linter is absent is worse than no gate. `ruff` is a "
            "declared `dev` dependency in pyproject.toml; install the dev group into the "
            f"environment running this suite ({sys.executable}).\n"
            f"interpreter directories searched:\n  {interpreter_dirs}\n"
            f"PATH searched:\n  {path_dirs}"
        )
    return found


def _run_ruff(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - absolute path from shutil.which, fixed argv, no shell
        [_ruff(), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _declared_ruff_requirements() -> dict[str, str]:
    """Every `ruff` requirement in pyproject, keyed by the table path that declares it.

    Derived by walking the parsed TOML rather than by grepping two known line numbers, so a
    third declaration added to a new table is covered the day it appears.
    """
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    tables: list[tuple[str, object]] = [
        ("project.dependencies", data.get("project", {}).get("dependencies")),
    ]
    for key, table in (("dependency-groups", data.get("dependency-groups", {})),
                       ("project.optional-dependencies",
                        data.get("project", {}).get("optional-dependencies", {}))):
        for group, requirements in table.items():
            tables.append((f"{key}.{group}", requirements))
    for where, requirements in tables:
        for requirement in requirements or []:
            if not isinstance(requirement, str):
                continue
            name = re.split(r"[<>=!~\[;\s]", requirement, maxsplit=1)[0]
            if name.strip().lower().replace("_", "-") == "ruff":
                found[where] = requirement
    return found


def test_every_declared_ruff_requirement_pins_one_exact_version():
    """`ruff` is pinned with `==`, everywhere it is declared, to the same version.

    Two genuinely different derivations, because one instrument cannot check itself: the TOML
    walk in `_declared_ruff_requirements` resolves requirements semantically, and the regex
    below counts them as raw text. If the walk silently stopped finding anything it would pass
    vacuously — the equality against the text count is what makes that impossible.
    """
    declared = _declared_ruff_requirements()
    textual = [m.group(0).strip() for m in
               _RUFF_REQUIREMENT.finditer(PYPROJECT.read_text(encoding="utf-8"))]
    assert len(declared) == len(textual), (
        f"the two derivations disagree about how many `ruff` requirements pyproject.toml "
        f"declares — TOML walk found {len(declared)} ({sorted(declared)}), raw text found "
        f"{len(textual)} ({textual}). One of them is blind; fix it before trusting either."
    )
    assert declared, "pyproject.toml declares no `ruff` requirement at all"

    unpinned = {where: req for where, req in declared.items()
                if not re.fullmatch(r"ruff(\[[^\]]*\])?==[0-9][^,;]*", req)}
    assert not unpinned, (
        f"`ruff` must be pinned with `==`, not a floor: {unpinned}. `[tool.ruff.lint].select` "
        f"lists family prefixes, so a release that adds a rule to any selected family reddens "
        f"`ruff check` on a tree nobody touched — and `ruff check` is gated by this file."
    )

    versions = {req.split("==", 1)[1] for req in declared.values()}
    assert len(versions) == 1, (
        f"the `ruff` declarations pin different versions {sorted(versions)} across "
        f"{sorted(declared)}; there is then no single answer to 'which ruff is this repo's'."
    )


def test_the_ruff_that_will_run_is_the_ruff_that_is_pinned():
    """The RESOLVED binary matches the pin — a declaration read is not a value exercised.

    Guardrail 6: reading `ruff==0.16.2` out of pyproject proves what was written, never what
    the environment installed. This runs `ruff --version` in the interpreter's own PATH, which
    is the one the gate below shells out to.
    """
    versions = {req.split("==", 1)[1].strip()
                for req in _declared_ruff_requirements().values() if "==" in req}
    assert len(versions) == 1, (
        f"there is no single pinned ruff version to compare the installed one against "
        f"(found {sorted(versions)}); "
        f"`test_every_declared_ruff_requirement_pins_one_exact_version` says why."
    )
    pinned = versions.pop()
    out = _run_ruff("--version")
    assert out.returncode == 0, f"`ruff --version` exited {out.returncode}: {out.stderr!r}"
    installed = out.stdout.strip().removeprefix("ruff").strip()
    assert installed == pinned, (
        f"pyproject.toml pins ruff=={pinned} but the `ruff` on PATH is {installed} "
        f"({_ruff()}). The gate below would then be measuring a different linter from the one "
        f"this repository declares; reinstall the dev group."
    )


def test_ruff_resolves_only_this_projects_own_files():
    """The gate's scope is asserted, not assumed — and it is asserted before it is reported.

    `ruff check .` builds its own file list, and part of how it builds it is by honouring
    `.gitignore`. That is what keeps `references/` (1660 read-only `.py` files) and the runtime
    write paths out of the gate. It is a default plus a file this gate does not own, so if it
    ever stops holding, the next `ruff check .` lints a thousand files nobody may modify and
    the gate becomes unfixable rather than merely red. Failing here instead says which paths
    escaped.
    """
    out = _run_ruff("check", "--no-cache", "--show-files", ".")
    assert out.returncode == 0, (
        f"`ruff check --show-files .` exited {out.returncode} — ruff could not resolve its own "
        f"file set, which is a configuration failure, not a lint finding:\n{out.stderr}"
    )
    resolved = [Path(line) for line in out.stdout.splitlines() if line.strip()]
    assert resolved, "`ruff check --show-files .` resolved no files at all"

    # How much work `.gitignore` is actually doing here — measured, not assumed, because it is
    # also this check's discriminating power. Disclosed rather than asserted: a worktree in
    # which the answer is 0 is a real environment a lane will be standing in, not a defect.
    unignored = _run_ruff("check", "--no-cache", "--no-respect-gitignore", "--show-files", ".")
    kept_out = (
        len([line for line in unignored.stdout.splitlines() if line.strip()]) - len(resolved)
        if unignored.returncode == 0
        else None
    )
    if kept_out == 0:
        warnings.warn(
            f"the scope check ran and cannot discriminate in this environment ({REPO_ROOT}): "
            f"`.gitignore` keeps 0 files out of `ruff check .` here, so a `.gitignore` "
            f"regression is undetectable by this check as run. A fresh detached worktree holds "
            f"none of the ignored trees — `references/*/` are separate git repositories that "
            f"exist only in a populated checkout, and `work/`, `cache/`, `mirrors/` and "
            f"`artifacts/` are runtime write paths — so green here is not evidence "
            f"that the gate is silent on a leak. Certify this check in a populated checkout.",
            stacklevel=2,
        )

    strays = []
    for path in resolved:
        relative = path.relative_to(REPO_ROOT) if path.is_absolute() else path
        if relative.parts[0] not in _OWNED_TREES and len(relative.parts) > 1:
            strays.append(str(relative))
    assert not strays, (
        f"`ruff check .` resolved {len(strays)} file(s) outside {_OWNED_TREES} and outside the "
        f"repository root: {sorted(strays)[:20]}. Either a new source tree was added and this "
        f"list needs it, or `.gitignore` stopped excluding read-only/runtime trees — in which "
        f"case the gate is about to lint code this project is forbidden to change."
    )


def test_ruff_check_is_clean_across_the_whole_repository():
    """`ruff check .` — WHOLE REPOSITORY, no path filter, no `--select` override, no cache.

    The scope is stated in that sentence on purpose. The claim this replaces was "ruff check is
    clean", made from a run scoped to one file and reported without the scope; it was true of
    the run and false of the repository, and it stayed false across two refs. `--no-cache`
    because a gate that can be satisfied by a stale cache entry is a detector that goes silently
    blind on exactly the file somebody just changed.
    """
    out = _run_ruff("check", "--no-cache", "--output-format=concise", ".")
    assert out.returncode == 0, (
        f"`ruff check --no-cache --output-format=concise .` exited {out.returncode} "
        f"(0=clean, 1=violations, 2=ruff itself failed), run from {REPO_ROOT}.\n"
        f"--- stdout ---\n{out.stdout}\n--- stderr ---\n{out.stderr}"
    )


# `ruff format --check --no-cache .` at `b9524af` (round R, task 3): 123 of 282 files dirty,
# reproduced identically across two independent runs. This is a BASELINE, not a clean-formatting
# target — see the module docstring for why reformatting is out of scope. Update this number,
# deliberately, in the same commit as whatever changes the dirty count (a reformat, a new file
# added dirty, or new format drift on an existing file).
# Note, added at `54b2c80` (round R final-fix): re-measured at this commit, 123 dirty / 160 clean
# / 283 scanned — the denominator moved (see module docstring note); the pinned count below did
# not.
# Note, added round VI task 116: re-measured against the `b9524af` file list directly (diffed,
# not re-derived from a report's stale count). 6 files went newly dirty and 1 (`tests/test_retry.
# py`, unmodified content, moved to `tests/unit/test_retry.py` by an unrelated sibling task) both
# left and re-entered the set — net +6, landing at 129 before this task's own fix. 3 of those 6
# were brand-new test files from this round's own work (`tests/test_baseline_container.py`,
# `tests/test_heavy_tier_outage_e2e.py`, `tests/test_stub_resolution_task79.py`) with narrow,
# self-contained format drift and no unrelated pre-existing content at stake — reformatted in
# place per this file's own stated policy, not pinned. The other 3 are pre-existing, already-
# committed `.superpowers/sdd/.../task-{78,80,105}-report.md` reports written by OTHER tasks,
# whose dirty code fences are verbatim historical quotations of exact code as it existed at named
# commits (e.g. "Before (pre-fix, `b4bc8be`)") — reformatting them would rewrite what those
# quotations show, which is out of scope for this task and contrary to the project's own
# annotate-never-rewrite convention for historical records. Those 3 are pinned instead, not
# reformatted.
# **Corrected 2026-09-10 (round VI controller, fortieth wave): 126 above was wrong the moment it
# landed — not a new drift, a parallel-merge collision.** Round VI task 115 (a sibling hotfix,
# reformatting-neutral on its own two touched files) merged to `main` between this pin's own
# branch point and this commit, changing the whole-repo file set `ruff format --check .` walks.
# Re-measured directly via this test's own exact command
# (`ruff format --check --no-cache --output-format=concise .`) against current `main`: **123**,
# not 126. The 3 pinned report files are independently re-confirmed still dirty for the same
# reason stated above (unchanged). Do not carry 126 forward; 123 is this pin's true current value.
# **Corrected 2026-09-13 (round VIII, worker-ruff-format-drift): the note directly above this one
# does not reproduce and was itself an unmeasured/wrong claim, left in place per this project's
# annotate-never-rewrite convention rather than edited.** Checking out `7f6ba2d` itself (the
# commit that wrote "123, not 126" above) and running this test's own exact command against that
# commit reads **126**, with the identical dirty-file set `bb8ec70` had — not 123. There was no
# "parallel-merge collision" changing the file set between those two commits; `bb8ec70`'s 126 was
# the correct measured value the whole time, and `7f6ba2d` reverted a correct pin to a wrong one.
# Separately, `334edeb` (round VIII) created `src/fleet/util/errors.py` dirty (never run through
# `ruff format`) and, in the same commit, incidentally left `src/fleet/llm/backends/
# openai_compatible.py` ruff-format-clean — net zero to the total (126 unchanged), but real
# per-file churn. This task reformatted `errors.py` (whitespace-only docstring collapse; `ruff
# check` and `mypy` both confirmed clean on it, no behavior change) — a genuine -1. Net: 126
# (`bb8ec70`'s true value, never actually 123) - 1 (this task's `errors.py` fix) = **125**, which
# is this pin's true current value. The 3 permanently-excluded report files
# (`.superpowers/sdd/round-VI-criteria-closure/task-{78,80,105}-report.md`) remain dirty and
# out of scope, per the original rationale above — confirmed by reading their content directly,
# they still contain verbatim historical code quotations (e.g. `Before (pre-fix, `b4bc8be`):`
# fenced blocks) that reformatting would rewrite.
# **Corrected 2026-09-13 (round VIII, worker-ruff-drift-2 follow-up, controller-directed
# structural fix): 125 above was the pin's true value only until this same round's
# `worker-citation-drift-fix-report.md` and then this task's own `worker-ruff-drift-2-report.md`
# each in turn tripped this same test via an illustrative python-fenced snippet inside a
# `.superpowers/` round-ledger report -- the third such report/pin interaction this round, per
# the controller. Rather than keep reformatting one report at a time, the controller ordered
# `.superpowers/` added to `pyproject.toml`'s `[tool.ruff]` `exclude` (round-ledger coordination
# scratch, never project source). That exclusion is a SCOPE change, not a drift: it removes the
# 3 permanently-excluded historical-quotation files named above from `ruff format`'s scan
# entirely (they are no longer walked at all, so there is nothing left to "confirm still dirty"),
# not just from this test's accounting of them. Re-measured directly post-exclude:
# `ruff format --check --no-cache --output-format=concise .` -> **122** dirty, and the per-file
# dirty list is IDENTICAL to the prior 125-file set minus exactly those 3 files (comm-diffed both
# ways: zero newly-dirty, exactly those 3 newly-clean-by-exclusion). 125 - 3 = 122; no other
# file's dirty status changed.
# Round VIII's §15.1 item 3 mutation-audit sweep (35 batches, 12 new test files + 21 extended)
# introduced format drift the sweep's own per-batch `ruff check` runs never caught, because none
# of them ran `ruff format --check` — only `ruff check` (a different tool pass). The final
# whole-round review caught this as new drift (133 dirty, up from 122) rather than pre-existing
# debt: 7 of the 12 brand-new files were unformatted. Fix: ran `ruff format` on every file this
# session touched (33 files, `git diff --stat <session-base>..main -- tests/`) rather than the
# whole repo (pre-existing dirty files outside this session's own footprint stay out of scope,
# per `docs/CRITERIA_PLAN.md` §2's done bar) — 20 of the 33 needed reformatting. Re-measured
# directly post-format: `ruff format --check --no-cache .` -> **113** dirty, net lower than the
# prior 122 baseline (this session's new files, once formatted, count as clean; no file outside
# this session's touched set changed status). No test behavior changed (formatting only).
_RUFF_FORMAT_DIRTY_BASELINE = 113

_FORMAT_PER_FILE = re.compile(
    r"^(?P<path>\S+):\d+:\d+: unformatted: File would be reformatted$", re.MULTILINE
)
_FORMAT_SUMMARY = re.compile(r"^(?P<count>\d+) files? would be reformatted\b", re.MULTILINE)


def test_ruff_format_check_dirty_count_matches_the_pinned_baseline():
    """`ruff format --check` dirty-file count is PINNED, not gated clean (module docstring).

    Two independent derivations of the same run, because one instrument cannot check itself
    (same pattern as `test_every_declared_ruff_requirement_pins_one_exact_version` above):
    `--output-format=concise` prints one `path:line:col: unformatted: ...` line per dirty file,
    counted by `_FORMAT_PER_FILE`; ruff's own summary line ("N files would be reformatted, ...")
    is counted separately by `_FORMAT_SUMMARY`. If they disagree, one of the two parsers is
    blind and neither dirty count can be trusted.

    Pinning is what makes this a genuine check rather than a description: without it, this test
    would pass no matter how many files are dirty, and a handful silently reformatted (or newly
    gone dirty) would be invisible. Whole repository, `--no-cache` for the same reason
    `test_ruff_check_is_clean_across_the_whole_repository` uses it — a stale cache entry would
    hide exactly the file somebody just changed.
    """
    out = _run_ruff("format", "--check", "--no-cache", "--output-format=concise", ".")
    assert out.returncode in (0, 1), (
        f"`ruff format --check --no-cache --output-format=concise .` exited {out.returncode} "
        f"(0=clean, 1=some files would be reformatted, anything else means ruff itself failed), "
        f"run from {REPO_ROOT}.\n--- stdout ---\n{out.stdout}\n--- stderr ---\n{out.stderr}"
    )

    per_file = _FORMAT_PER_FILE.findall(out.stdout)
    summary_match = _FORMAT_SUMMARY.search(out.stdout)
    summary_count = int(summary_match.group("count")) if summary_match else 0

    assert len(set(per_file)) == len(per_file), (
        f"the per-file derivation found the same path more than once, so it is not one line "
        f"per dirty file as assumed: {sorted(p for p in per_file if per_file.count(p) > 1)}\n"
        f"--- stdout ---\n{out.stdout}"
    )
    assert len(per_file) == summary_count, (
        f"two derivations of the same `ruff format --check` run disagree: {len(per_file)} "
        f"per-file 'unformatted' line(s) vs {summary_count} in ruff's own summary line. One of "
        f"them is blind; fix the parser before trusting either count.\n"
        f"--- stdout ---\n{out.stdout}"
    )

    assert summary_count == _RUFF_FORMAT_DIRTY_BASELINE, (
        f"`ruff format --check` now reports {summary_count} dirty file(s); the pinned baseline "
        f"is {_RUFF_FORMAT_DIRTY_BASELINE}. This is deliberately a BASELINE, not a clean-format "
        f"gate (module docstring) — reformatting the dirty files is out of scope per "
        f"`docs/CRITERIA_PLAN.md` §2's done bar. But an unpinned count hides drift silently in "
        f"either direction: fewer dirty files means someone reformatted without this test "
        f"noticing (update the pin down), more means a change introduced new format drift "
        f"(investigate before updating the pin up). Re-measure with "
        f"`ruff format --check --no-cache .` from {REPO_ROOT} and update "
        f"`_RUFF_FORMAT_DIRTY_BASELINE` deliberately, never to silence this failure.\n"
        f"dirty files (first 20 of {len(per_file)}): {sorted(per_file)[:20]}"
    )


def _mypy() -> str:
    """The absolute path of the `mypy` that will run — same resolution strategy as `_ruff()`.

    PATH exactly as `tests/conftest.py` left it, then the running interpreter's own `bin/`, so a
    detached worktree with no `.venv` of its own still finds the `mypy` beside `sys.executable`
    rather than nothing. See `_ruff()`'s docstring for why the interpreter's path is not
    `.resolve()`-d (a `.venv/bin/python` symlink would resolve to the system interpreter's
    directory, which is not where console scripts live).

    Absence still fails loudly, naming everywhere searched — `mypy` is a declared `dev`
    dependency, so a missing one is a broken environment, not weather.
    """
    candidates = [sysconfig.get_path("scripts"), str(Path(sys.executable).parent)]
    found = shutil.which("mypy")
    for candidate in candidates:
        if found is None:
            found = shutil.which("mypy", path=candidate)
    if found is None:
        interpreter_dirs = "\n  ".join(candidates)
        path_dirs = "\n  ".join(os.get_exec_path())
        pytest.fail(
            "`mypy` is nowhere this gate can find it, so the mypy gate cannot run — and a gate "
            "that passes when its checker is absent is worse than no gate. `mypy` is a declared "
            "`dev` dependency in pyproject.toml; install the dev group into the environment "
            f"running this suite ({sys.executable}).\n"
            f"interpreter directories searched:\n  {interpreter_dirs}\n"
            f"PATH searched:\n  {path_dirs}"
        )
    return found


_MYPY_SUCCESS = re.compile(r"Success: no issues found in (?P<count>\d+) source files?")


def test_mypy_strict_is_clean_over_src_fleet():
    """`mypy src/fleet/ --strict` exits 0 — the invocation `docs/INTEGRATION_HONESTY.md`'s
    checkpoints have run out-of-band since the `ast-grep` round (`mypy src/fleet/ --strict`
    clean over 106 files, later 107+), now asserted in-tree instead of only in a checkpoint doc.

    `cwd=REPO_ROOT` matters here, not just for consistency with `_run_ruff`: `[tool.mypy]
    mypy_path = "src"` in pyproject.toml resolves relative to the process's CWD, not to
    `pyproject.toml`'s own directory (`CLAUDE.md` §6's documented mypy gotcha) — run with cwd
    outside the worktree and this could silently fall back to resolving an installed `fleet`
    package instead of this worktree's own `src/`.

    The file count in mypy's own "Success" line is asserted positive, not just the exit code:
    an exit-0 mypy run that checked zero files (wrong `cwd`, wrong path argument, a broken
    `mypy_path`) is the vacuous-pass shape this project's own instruments are written to catch,
    and an exit-code-only assertion cannot tell that apart from a genuine clean run.
    """
    out = subprocess.run(  # noqa: S603 - absolute path from shutil.which, fixed argv, no shell
        [_mypy(), "src/fleet/", "--strict"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, (
        f"`mypy src/fleet/ --strict` exited {out.returncode}, run from {REPO_ROOT} "
        f"(using {_mypy()}):\n--- stdout ---\n{out.stdout}\n--- stderr ---\n{out.stderr}"
    )
    match = _MYPY_SUCCESS.search(out.stdout)
    assert match, (
        f"mypy exited 0 but its stdout does not confirm how many files it actually checked, so "
        f"a 0-returncode from a run that resolved zero files (wrong cwd, wrong `mypy_path`) "
        f"cannot be told apart from a genuine clean run over this worktree's own `src/fleet/`:\n"
        f"--- stdout ---\n{out.stdout}"
    )
    checked = int(match.group("count"))
    assert checked > 0, (
        f"`mypy src/fleet/ --strict` reported checking {checked} source files — that is vacuous, "
        f"not clean.\n--- stdout ---\n{out.stdout}"
    )


def _uv() -> str:
    """The absolute path of the `uv` that will run — same resolution strategy as `_ruff()`.

    Unlike `ruff`/`mypy`, `uv` is NOT a declared dependency anywhere in `pyproject.toml` or
    `uv.lock` (checked by grep before writing this): it is the external tool that *produces*
    `uv.lock`, analogous to `bazel`/`git` rather than to a `dev` group package. So absence here
    is reported as "install `uv` itself", not "reinstall the dev group" — the latter would be a
    false instruction for this specific tool. See `_ruff()`'s docstring for why PATH-then-
    interpreter's-own-`bin/` is searched in that order, and why the interpreter's directory is
    not `.resolve()`-d.
    """
    candidates = [sysconfig.get_path("scripts"), str(Path(sys.executable).parent)]
    found = shutil.which("uv")
    for candidate in candidates:
        if found is None:
            found = shutil.which("uv", path=candidate)
    if found is None:
        interpreter_dirs = "\n  ".join(candidates)
        path_dirs = "\n  ".join(os.get_exec_path())
        pytest.fail(
            "`uv` is nowhere this gate can find it, so the offline-sync gate (SPEC §12 item 1) "
            "cannot run. `uv` is not a declared Python dependency of this project — it is the "
            f"external tool that produced `uv.lock` — install it into ({sys.executable}).\n"
            f"interpreter directories searched:\n  {interpreter_dirs}\n"
            f"PATH searched:\n  {path_dirs}"
        )
    return found


def test_uv_sync_frozen_is_exit_0_offline_on_py312():
    """SPEC §12 item 1: `uv sync --frozen` against the committed `uv.lock`, network disabled,
    exits 0 — and the interpreter running this suite is (3, 12), the criterion's second clause.

    **Why the subprocess passes `--python sys.executable`.** The `sys.version_info` assertion
    below only proves the *pytest runner's own* interpreter is 3.12 — it says nothing about which
    interpreter `uv sync` itself resolves to, since `uv`'s interpreter search is independent of
    the calling process. Without pinning, a host where `uv` finds a different Python first could
    green this test while the sync it measures ran under the wrong version, and SPEC's clause
    names 3.12 for the *sync*, not for the runner. Passing `--python sys.executable` closes that
    gap by forcing `uv sync` onto the exact interpreter already asserted to be 3.12.

    **Why `--offline`, not some other network-disabling mechanism.** Grepped first (this
    docstring records the negative result): nothing else in `tests/` disables network access
    for a *subprocess* — every other "offline"/"no socket" test in this codebase (e.g.
    `test_llm_backend_anthropic.py`, `test_run_context_llm_cache.py`) achieves it by
    monkeypatching or faking a Python-level transport in-process, which has no subprocess to
    apply to here. So this uses `uv`'s own documented flag: `--offline` ("Disable network
    access", `env: UV_OFFLINE=`, per `uv help sync`). `--frozen` ("Sync without updating the
    `uv.lock` file", `env: UV_FROZEN=`) is passed explicitly rather than relied on as a default,
    matching the criterion's literal wording and this file's convention of spelling out every
    flag a gate depends on rather than a tool's default behaviour.

    **Why this test does NOT run against the shared `.venv`, and cannot even by accident.**
    `uv sync`'s target is normally the project-relative `.venv` next to `pyproject.toml` — safe
    in a worktree (a worktree has no `.venv` of its own, `tests/conftest.py`'s module docstring),
    but this file is also the primary checkout's own test file, and running it there with no
    further care would create/update `REPO_ROOT/.venv` — the SHARED environment other rounds and
    sessions depend on, which CLAUDE.md's task brief explicitly forbids risking. Two independent
    guards, so neither alone has to be trusted: (1) `VIRTUAL_ENV` is popped from the subprocess
    environment, which forecloses the one flag (`--active`, not passed here) that would target an
    ambient active venv; (2) `UV_PROJECT_ENVIRONMENT` is set to a fresh `tempfile.mkdtemp()`
    directory for the duration of the call and removed after — an *absolute* path, which per
    `uv`'s own docs is used as-is rather than nested under the project root, so the sync's target
    can never resolve to `REPO_ROOT/.venv` regardless of which checkout `REPO_ROOT` is. Verified
    empirically before writing this test: with this env override, `uv sync --frozen --offline`
    created and populated only the throwaway directory, and `git status --porcelain` in
    `REPO_ROOT` before and after was identical (no `uv.lock` rewrite, no stray file).

    **Why offline can be expected to succeed at all: a documented, out-of-band precondition.**
    `uv sync --frozen --offline` needs every wheel `uv.lock` names already present in `uv`'s own
    package cache (`uv cache dir`, independent of any particular `.venv`) — offline cannot
    populate a cold cache. This is exactly the same shape as `_ruff()`/`_mypy()`'s documented
    precondition ("install the dev group into the environment running this suite"): a one-time,
    network-using `uv sync --frozen` (no `--offline`) warms that cache; this test then measures
    whether a *second*, network-disabled sync from the now-warm cache is reproducible — which is
    the property SPEC §12 item 1 actually names. The test does not warm the cache itself, on
    purpose: doing so would use the network inside a test whose entire point is proving no
    network is needed.

    **Mutation proving this discriminates, not vacuous:** with a throwaway copy of `uv.lock`
    where one package's pinned version was changed to a value inconsistent with its own wheel
    filename (`mypy` `2.3.1` -> `999.999.999`, wheel filename left naming `2.3.1`), the identical
    `uv sync --frozen --offline` invocation exited 2 ("Failed to parse `uv.lock` ... malformed
    wheel") instead of 0 — reproduced by hand before writing this assertion, `uv.lock` restored
    via `git checkout -- uv.lock` immediately after. A corrupt lock reddens this test; the
    committed one greens it.
    """
    assert sys.version_info[:2] == (3, 12), (
        f"this test suite is running under Python {sys.version_info[:2]}, not (3, 12) — SPEC "
        f"§12 item 1's second clause names the interpreter directly, and it is "
        f"{sys.executable} that is under test."
    )

    with tempfile.TemporaryDirectory(prefix="fleet-uv-sync-offline-") as throwaway_env:
        env = dict(os.environ)
        env.pop("VIRTUAL_ENV", None)
        env["UV_PROJECT_ENVIRONMENT"] = throwaway_env
        out = subprocess.run(  # noqa: S603 - absolute path from shutil.which, fixed argv
            [_uv(), "sync", "--frozen", "--offline", "--python", sys.executable],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        assert out.returncode == 0, (
            f"`uv sync --frozen --offline` exited {out.returncode}, run from {REPO_ROOT} "
            f"(using {_uv()}) against throwaway UV_PROJECT_ENVIRONMENT={throwaway_env} "
            f"(0=synced clean, nonzero=either the lock/environment drifted or the local `uv` "
            f"wheel cache is cold — see this test's docstring for the cache-warming "
            f"precondition).\n--- stdout ---\n{out.stdout}\n--- stderr ---\n{out.stderr}"
        )
