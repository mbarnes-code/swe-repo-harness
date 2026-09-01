"""§11.5 step 8's plan and its delegation — `cli._continue_from_floors` / `_continue_impl`.

**Rule 12 note, per case rather than per count.** Each test below is the UNIQUE discriminator of
at least one mutation of the code under test; the mutation is named in the test's own docstring
so a later author can re-run it rather than re-derive it. Cases that only duplicated another
case's discrimination were not written. The full mutation battery, its zero-change gate and the
per-case verdicts live in this lane's report; the docstrings are the durable half.

Nothing here touches SQLite, git, docker or the network: the three phase composition roots are
replaced with recorders, which is the whole point of step 8 delegating to them rather than
composing a fifth `PhaseRunner`.
"""

from __future__ import annotations

import ast
import fcntl
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from fleet import cli
from fleet.cli import ExitCode, FleetCliError, HumanInterventionError, UsageError, _Continuation
from fleet.models.enums import Phase

REPO_ROOT = Path(__file__).resolve().parents[1]


def _payload(exit_code: int = ExitCode.SUCCESS, halt: str = "") -> dict[str, object]:
    """The subset of a phase payload `_raise_for_phase` reads (`cli._raise_for_phase`)."""
    return {"exit_code": int(exit_code), "halt": halt, "failed": 0, "attention": []}


def _settings(tmp_path: Path) -> Any:
    """Just enough of `FleetSettings` for `cli._mirror_lock_path` to resolve a real path."""
    monorepo = tmp_path / "monorepo"
    (monorepo / ".git").mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        root=tmp_path, config=SimpleNamespace(run=SimpleNamespace(monorepo_path="monorepo"))
    )


async def _drive(
    monkeypatch: pytest.MonkeyPatch,
    floors: dict[str, Phase],
    *,
    only: str | None = None,
    settings: Any = None,
    results: dict[Phase, dict[str, object]] | None = None,
    trace: list[Any] | None = None,
    patch_mutex: bool = True,
) -> dict[str, object]:
    """Run `_continue_impl` with the three roots replaced by recorders.

    Every per-phase knob is supplied explicitly here for the same reason `_continue_impl` makes
    them required: a default chosen in a test is a default that can leak into the caller.

    **`_require_disk_headroom` is stubbed, and that is a repair, not a convenience.** `94a2653`
    added the §12.22 gate to `_continue_impl`; this fixture's settings stand-in is a
    `SimpleNamespace` with no `config.run.cache_dir`, so from that commit every driver case here
    died in `_gc_disk` with `AttributeError` — five of them, red on `main`, for a reason that has
    nothing to do with what they assert. It is stubbed to a plain no-op rather than to a
    `calls` recorder so the ordering assertions on `calls[0]` keep meaning what they say; the
    gate's own behaviour is covered end to end by
    `test_the_continuation_takes_the_disk_headroom_gate_before_its_first_delegate`
    (`tests/test_cli.py`), which drives the real settings object.
    """
    calls = trace if trace is not None else []
    results = results or {}

    def recorder(phase: Phase) -> Any:
        async def stub(*args: object, **kwargs: object) -> dict[str, object]:
            calls.append((phase, kwargs))
            return results.get(phase, _payload())

        return stub

    monkeypatch.setattr(cli, "_transform_impl", recorder(Phase.TRANSFORM))
    monkeypatch.setattr(cli, "_build_impl", recorder(Phase.BUILD))
    monkeypatch.setattr(cli, "_verify_impl", recorder(Phase.VERIFY))
    monkeypatch.setattr(cli, "_require_disk_headroom", lambda *a, **k: {})
    if patch_mutex:
        monkeypatch.setattr(
            cli, "_refuse_concurrent_mirror_run", lambda *a, **k: calls.append("MUTEX")
        )
    return await cli._continue_impl(
        SimpleNamespace(),
        settings if settings is not None else SimpleNamespace(),
        Path("/nonexistent/fleet.db"),
        run_id="run-1",
        floors=floors,
        only=only,
        ladder=1,
        dry_run=False,
        timeout_s=600,
        sandboxed=True,
        rdeps_limit=2000,
        rdeps_sample_n=500,
        affected_only=True,
    )


# --------------------------------------------------------------------------------------
# 10a — the pure plan
# --------------------------------------------------------------------------------------


def test_the_plan_groups_by_phase_in_ascending_phase_order() -> None:
    """Ascending PHASE order, whatever order the floors mapping happens to iterate in.

    Unique discriminator of: `sorted(grouped)` -> `grouped` in `_continue_from_floors`. The
    input's insertion order is VERIFY, TRANSFORM, BUILD precisely so the mutation is expressible;
    each group's repos are already sorted here so the sibling `sorted(grouped[phase])` mutation
    cannot redden this case instead.
    """
    plan = cli._continue_from_floors(
        {"v": Phase.VERIFY, "t": Phase.TRANSFORM, "b": Phase.BUILD, "t2": Phase.TRANSFORM}, None
    )
    assert [entry.phase for entry in plan] == [Phase.TRANSFORM, Phase.BUILD, Phase.VERIFY]
    assert plan == (
        _Continuation(phase=Phase.TRANSFORM, repos=("t", "t2")),
        _Continuation(phase=Phase.BUILD, repos=("b",)),
        _Continuation(phase=Phase.VERIFY, repos=("v",)),
    )


def test_the_repos_in_a_group_are_sorted_and_the_entry_is_frozen() -> None:
    """Repo order inside a group is the repo id's, not the mapping's.

    Unique discriminator of: `tuple(sorted(grouped[phase]))` -> `tuple(grouped[phase])`. One
    floored phase only, so the ascending-phase mutation is a no-op here and cannot claim this
    case. It deliberately asserts the FLOORED entry alone and not the span above it — the span
    cases below own that, and pinning it here as well would leave them discriminating nothing.
    """
    plan = cli._continue_from_floors({"z": Phase.BUILD, "a": Phase.BUILD, "m": Phase.BUILD}, None)
    assert plan[0] == _Continuation(phase=Phase.BUILD, repos=("a", "m", "z"))
    with pytest.raises((AttributeError, TypeError)):
        plan[0].repos = ()  # type: ignore[misc]


def test_only_filters_with_fnmatch_and_the_excluded_repo_is_in_no_group() -> None:
    """`only` is the delegates' predicate (`fnmatch`, as `cli._wave_repos` uses), applied here.

    What `only` takes from `other` is MEMBERSHIP, not the phase: `acme-web` is floored at
    `TRANSFORM`, so VERIFY is in the span and is driven either way. An excluded repo can neither
    pull the span down nor keep its own phase out of it.

    Unique discriminator of two mutations: dropping the `only` guard altogether (then `other`
    survives into the VERIFY group), and `fnmatch(repo_id, only)` -> `repo_id == only` (then
    nothing matches the glob, there is no servable floor to span from, and the plan is empty).
    It is the only case that passes `only`, so no other case can express either. It asserts
    membership and an exclusion rather than the whole tuple, so that the span cases below keep
    their own discrimination.
    """
    floors = {"acme-web": Phase.TRANSFORM, "acme-api": Phase.BUILD, "other": Phase.VERIFY}
    plan = cli._continue_from_floors(floors, "acme-*")
    assert _Continuation(phase=Phase.TRANSFORM, repos=("acme-web",)) in plan
    assert _Continuation(phase=Phase.BUILD, repos=("acme-api",)) in plan
    assert all("other" not in entry.repos for entry in plan)


def test_a_scan_floor_is_a_group_in_the_plan_like_any_other() -> None:
    """The PLAN is total; the SKIP is the driver's decision (ADR-0080 ruling B).

    `phase_floor` really can return `Phase.SCAN`, and `computed_floors` is written before
    `demotable_phases` runs, so it retains every repo step 5 did NOT demote.

    Unique discriminator of: moving the skip UP into `_continue_from_floors` (`if floor is
    Phase.SCAN: continue`) while keeping the driver's payload key correct by deriving `skipped`
    from `floors` directly. Under that refactor every driver case below stays green and only
    this one reddens — which is exactly why it is worth a case.

    It asserts MEMBERSHIP rather than the whole tuple, and that is deliberate: the span cases
    below own the shape of the plan, and pinning it here as well would leave them discriminating
    nothing of their own.
    """
    plan = cli._continue_from_floors({"unstarted": Phase.SCAN, "w": Phase.BUILD}, None)
    assert _Continuation(phase=Phase.SCAN, repos=("unstarted",)) in plan
    assert _Continuation(phase=Phase.BUILD, repos=("w",)) in plan


# --------------------------------------------------------------------------------------
# 10a — the SPAN: the plan is the phases from the lowest floor UPWARD, not the floors present
# --------------------------------------------------------------------------------------


def test_the_plan_spans_a_phase_no_repo_is_floored_at_between_two_that_are() -> None:
    """The defect C1 records: the plan was the SET of floors, so a gap between two floors was
    never driven, and `fleet resume` transformed a repo it never built — at exit 0.

    Each delegate drives exactly ONE phase and nothing walks the ladder, so a phase the plan
    omits is not run for anybody. With `{A: TRANSFORM, B: VERIFY}` the pre-fix plan was
    `[TRANSFORM, VERIFY]`: `A` ends `TRANSFORM = SUCCEEDED`, `BUILD = PENDING`, `_verify_impl`
    cannot admit it, nothing halts, `halted_phase` is `None` and the verb reports success.

    **The floors are deliberately NON-CONTIGUOUS and deliberately unequal.** Every fixture in
    this file before this one was contiguous upward from its own minimum, which is why the whole
    file was GREEN under the defect: a contiguous fixture is blind to it by construction, and a
    fixture whose repos share one floor is blind for the same reason.

    Unique discriminator of: filling only ABOVE the highest floor rather than across the span
    (`if phase >= lowest` -> `if phase > max(servable_floors)`). Every sibling keeps passing
    under that mutation — `{A: TRANSFORM}` still fills BUILD and VERIFY above its own maximum,
    and the `only` case above still fills VERIFY above `BUILD` — because this is the only case
    whose missing phase sits BETWEEN two present floors rather than above them all.

    It compares a `{phase: repos}` MAPPING rather than the ordered tuple, for the reason the
    ascending-order case above gives: the order is that case's to pin, and pinning it twice
    would leave it discriminating nothing of its own.
    """
    plan = cli._continue_from_floors({"A": Phase.TRANSFORM, "B": Phase.VERIFY}, None)
    assert {entry.phase: entry.repos for entry in plan} == {
        Phase.TRANSFORM: ("A",),
        Phase.BUILD: (),
        Phase.VERIFY: ("B",),
    }


def test_a_scan_floor_does_not_pull_the_span_down_to_phases_it_was_skipped_for() -> None:
    """The span is measured over the SERVABLE floors. A `SCAN` floor is reported and skipped
    (ADR-0080 §3), so it is not a repo the span exists to carry.

    Spanning up from `SCAN` here would plan `TRANSFORM` and `BUILD` on a fleet whose only
    sub-`VERIFY` repo was never scanned — spend for a beneficiary that was skipped, and an
    invitation to transform un-scanned work.

    Unique discriminator of: taking the span's lower end from every floor rather than from the
    servable ones (`lowest = min(servable_floors)` -> `lowest = min(grouped)`), which yields
    `[SCAN, TRANSFORM, BUILD, VERIFY]` here. It is the only case with a `SCAN` floor BELOW a
    servable one: the all-`SCAN` case has no servable floor at all so the fill never runs, and
    the `{SCAN, BUILD}` case above asserts membership, which extra groups cannot break.

    It asserts the two ABSENCES and the VERIFY group, and deliberately not the `SCAN` group —
    the case above owns that, and asserting it here too would take its discrimination away.
    """
    plan = cli._continue_from_floors({"unstarted": Phase.SCAN, "B": Phase.VERIFY}, None)
    planned = {entry.phase for entry in plan}
    assert Phase.TRANSFORM not in planned, "the SCAN floor pulled the span down to TRANSFORM"
    assert Phase.BUILD not in planned, "the SCAN floor pulled the span down to BUILD"
    assert _Continuation(phase=Phase.VERIFY, repos=("B",)) in plan


# --------------------------------------------------------------------------------------
# 10b — the delegation
# --------------------------------------------------------------------------------------


async def test_the_driver_hands_every_delegate_the_glob_and_the_whole_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each planned phase is delegated once, with `only` passed through and `wave=None`.

    `wave=None` is not incidental: step 8 continues a RUN, not a wave, and a wave filter here
    would silently strip work the floors say is outstanding.

    Unique discriminator of: `only=only` -> `only=None` on any delegate, and `wave=None` ->
    `wave=0`. It is the only case that inspects delegate kwargs. It deliberately asserts the SET
    of delegated phases rather than their order — the order is the plan's, and pinning it twice
    would leave the plan-ordering case discriminating nothing of its own.
    """
    calls: list[Any] = []
    await _drive(
        monkeypatch,
        {"acme-v": Phase.VERIFY, "acme-t": Phase.TRANSFORM, "acme-b": Phase.BUILD},
        only="acme-*",
        trace=calls,
    )
    delegated = [entry for entry in calls if entry != "MUTEX"]
    assert sorted(phase for phase, _ in delegated) == [
        Phase.TRANSFORM,
        Phase.BUILD,
        Phase.VERIFY,
    ]
    for _phase, kwargs in delegated:
        assert kwargs["only"] == "acme-*"
        assert kwargs["wave"] is None
        assert kwargs["run_id"] == "run-1"


async def test_the_driver_really_drives_the_phases_above_the_only_floor_there_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The WORK, not the exit code. C1's signature is exit 0 with a phase skipped, so a case
    asserting a non-zero exit code can pass for the wrong reason; this asserts the delegates
    above the floor were actually ENTERED, and that the continuation still ends clean.

    The fixture is C1's degenerate shape, and it is not adversarial at all: a fleet whose every
    floor is `TRANSFORM` — the ordinary state after a crash during transform. Pre-fix this drove
    `TRANSFORM` alone, so the fleet was transformed, never built, never verified, and `fleet
    resume` exited 0. Both phases above the floor have EMPTY groups here, which is what carries
    them.

    Unique discriminator of two mutations: bounding the span at the highest floor rather than at
    `_SERVABLE_PHASES`'s last entry (nothing above `TRANSFORM` is planned), and dropping the
    span's empty groups back out at the DRIVER (`if entry.phase in _SERVABLE_PHASES` ->
    `if entry.phase in _SERVABLE_PHASES and entry.repos`) — the plausible "why drive a phase
    with no repos" optimisation, which re-opens C1 in full while every plan case above stays
    green, because the plan they inspect is correct. No other driver case has an empty group for
    either to reach. It asserts the SET of delegated phases, not their order: the order is the
    plan's.
    """
    calls: list[Any] = []
    result = await _drive(monkeypatch, {"A": Phase.TRANSFORM}, trace=calls)
    assert sorted(entry[0] for entry in calls if entry != "MUTEX") == [
        Phase.TRANSFORM,
        Phase.BUILD,
        Phase.VERIFY,
    ], "a repo floored at TRANSFORM was transformed and never built or verified"
    assert set(result["driven"]) == {"TRANSFORM", "BUILD", "VERIFY"}  # type: ignore[arg-type]
    assert result["halted"] is None


async def test_the_driver_stops_at_the_first_halt(monkeypatch: pytest.MonkeyPatch) -> None:
    """A halted phase ends the continuation; the phases above it are never driven.

    Unique discriminator of: deleting the `break` after a non-`SUCCESS` delegate. It is the only
    case whose delegates do not all return `SUCCESS`.
    """
    result = await _drive(
        monkeypatch,
        {"t": Phase.TRANSFORM, "b": Phase.BUILD, "v": Phase.VERIFY},
        results={Phase.BUILD: _payload(ExitCode.REQUIRES_HUMAN_INTERVENTION, halt="stopped")},
    )
    assert result["driven"] == ["TRANSFORM", "BUILD"]
    assert result["halted_phase"] == "BUILD"
    assert result["halted"] == _payload(ExitCode.REQUIRES_HUMAN_INTERVENTION, halt="stopped")
    assert set(result["phase_results"]) == {"TRANSFORM", "BUILD"}  # type: ignore[arg-type]


async def test_a_scan_floor_is_named_in_the_payload_and_never_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0080 ruling B's success criterion: the `SCAN` population is LOUD, not dropped.

    The population is much larger than it looks — `computed_floors` retains every repo step 5
    did not demote, so on a fleet with un-started repos step 8's input is `SCAN`-heavy. An
    operator whose fleet does not move needs this key to diagnose it in five seconds.

    Unique discriminator of: `result[_SCAN_SKIPPED_KEY] = list(skipped)` -> `= []`, the silent
    drop the ruling exists to forbid. It is the only case that reads the key's CONTENT, which is
    deliberate — its sibling below owns the "nothing servable" shape and asserting the key there
    too would leave this case, the ruling's success criterion, discriminating nothing alone. The
    floors are seeded in sorted order so the repo-ordering mutation cannot claim it either.

    The `driven` assertion is deliberately the WEAK form — "`SCAN` is not among the phases
    driven" rather than an exact list. The exact list moves with the span above this floor,
    which the span cases own; asserting it here would make this case redden under every span
    mutation and leave those cases discriminating nothing of their own.
    """
    result = await _drive(
        monkeypatch, {"unstarted-a": Phase.SCAN, "w": Phase.BUILD, "unstarted-b": Phase.SCAN}
    )
    assert result["scan_floor_not_continued"] == ["unstarted-a", "unstarted-b"]
    assert "SCAN" not in cast("list[str]", result["driven"]), "a SCAN floor was served"
    assert result["halted"] is None


async def test_an_all_scan_plan_reports_the_skip_and_takes_no_mutex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing servable: report, drive nothing, and do NOT refuse.

    Refusing here would withhold the one diagnosis the operator needs, so the mirror guard is
    taken only when there is a first delegate for it to precede.

    Unique discriminator of: hoisting `_refuse_concurrent_mirror_run(...)` above the
    `if not servable: return result` early exit. Every other case has a servable phase, so the
    hoist is a no-op for them. The key's CONTENT is pinned by the sibling case above, not here.
    """
    calls: list[Any] = []
    result = await _drive(monkeypatch, {"a": Phase.SCAN, "b": Phase.SCAN}, trace=calls)
    assert result["driven"] == []
    assert calls == []


async def test_the_mirror_mutex_is_taken_once_before_the_first_delegate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0080 ruling C. Once — a per-delegate check opens a window between phases.

    The guard is taken by the `transform`/`build`/`verify` COMMANDS (via `_phase_preflight`) and
    not by the `_impl`s, so a continuation that delegates straight to the impls bypasses it.

    Unique discriminator of: moving the guard inside the delegate loop (three acquisitions
    instead of one). No other case asserts the interleaving of guard and delegates.
    """
    calls: list[Any] = []
    await _drive(
        monkeypatch, {"t": Phase.TRANSFORM, "b": Phase.BUILD, "v": Phase.VERIFY}, trace=calls
    )
    assert calls[0] == "MUTEX"
    assert calls.count("MUTEX") == 1


async def test_the_continuation_refuses_while_another_live_run_owns_the_mirror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end through the REAL guard: a held lock refuses, and no delegate runs.

    Unique discriminator of: swallowing the guard's `UsageError` in `_continue_impl` (e.g.
    wrapping the call in `contextlib.suppress`). Every other mutex case replaces the guard with
    a recorder that never raises and is blind to that by construction.

    **`exit_code == 2` is asserted separately from the `pytest.raises` match**, because SPEC §12
    item 48 states the exit code itself ("exits 2 on the `integration:<run_id>` mutex"), not just
    that a message containing the words "mirror mutex" is raised -- `UsageError.exit_code` is a
    class attribute (`cli.py::UsageError`, `exit_code = ExitCode.USAGE`), so this is a real
    discriminator of a hypothetical future refusal that raised the right message under the wrong
    exception type (any `FleetCliError` subclass with a different `exit_code`, e.g.
    `HumanInterventionError`'s 7) and would still satisfy the `match=` alone.
    """
    settings = _settings(tmp_path)
    lock = cli._mirror_lock_path(settings, "run-1")
    lock.touch()
    held = os.open(lock, os.O_RDWR)
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    calls: list[Any] = []
    try:
        with pytest.raises(UsageError, match="mirror mutex") as excinfo:
            await _drive(
                monkeypatch,
                {"t": Phase.TRANSFORM},
                settings=settings,
                trace=calls,
                patch_mutex=False,
            )
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)
    assert calls == []
    assert excinfo.value.exit_code == ExitCode.USAGE == 2


# --------------------------------------------------------------------------------------
# the exit code, and the two structural properties step 8 must not break
# --------------------------------------------------------------------------------------


def test_the_exit_code_is_the_halting_delegates_read_through_raise_for_phase() -> None:
    """§10's table is `_raise_for_phase`'s; step 8 reads it off the halting payload.

    Unique discriminator of: making `_raise_for_continuation` a no-op (`return` first), and
    reading the code off the continuation payload instead of the halted delegate's.
    """
    assert cli._raise_for_continuation({"halted": None}) is None
    with pytest.raises(HumanInterventionError):
        cli._raise_for_continuation(
            {"halted": _payload(ExitCode.REQUIRES_HUMAN_INTERVENTION)}
        )
    with pytest.raises(FleetCliError) as excinfo:
        cli._raise_for_continuation({"halted": _payload(ExitCode.WAVE_COST_EXHAUSTED, "broke")})
    assert excinfo.value.exit_code == ExitCode.WAVE_COST_EXHAUSTED


def _function_nodes() -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    tree = ast.parse((REPO_ROOT / "src" / "fleet" / "cli.py").read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def test_step_8_is_not_a_fourth_site_pairing_the_two_phase_raises() -> None:
    """Derived from the BODY, not from a name or a file region.

    `HumanInterventionError` beside a `FleetCliError(..., exit_code=...)` is §10's table written
    out; there are three such functions and a fourth copy is a fourth place the table can rot.
    Rather than pinning a global count another lane may legitimately move, this asserts the
    property OF step 8: neither step-8 function names `HumanInterventionError` at all, and the
    one that decides the exit code reaches it only by calling `_raise_for_phase`.

    Unique discriminator of: inlining `_raise_for_phase`'s two raises into
    `_raise_for_continuation`. No behavioural case can see that — the exit codes are identical.
    """
    functions = _function_nodes()
    for name in ("_continue_impl", "_raise_for_continuation"):
        raised = {
            node.exc.func.id
            for node in ast.walk(functions[name])
            if isinstance(node, ast.Raise)
            and isinstance(node.exc, ast.Call)
            and isinstance(node.exc.func, ast.Name)
        }
        assert "HumanInterventionError" not in raised, f"{name} became a fourth §10 table"
    called = {
        node.func.id
        for node in ast.walk(functions["_raise_for_continuation"])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_raise_for_phase" in called


def test_step_8_composes_no_phase_runner_of_its_own() -> None:
    """Step 8 RE-ENTERS the three roots; it does not build a fifth `PhaseRunner`.

    A continuation composing its own runner would be a second composition that has to be kept in
    step with the phase verbs' by inspection — which is what delegating to the roots avoids.

    Unique discriminator of two mutations: instantiating a `PhaseRunner` inside `_continue_impl`,
    and merely NAMING it there. Both are checked because a name-load is the cheaper escape and an
    earlier draft of this test, keyed on `ast.Call` alone, certified the bare-name mutation GREEN.
    Every behavioural case replaces the three roots with recorders and is blind to both.
    """
    functions = _function_nodes()
    mentioned = {
        node.id for node in ast.walk(functions["_continue_impl"]) if isinstance(node, ast.Name)
    }
    assert "PhaseRunner" not in mentioned
    assert {"_transform_impl", "_build_impl", "_verify_impl"} <= mentioned
