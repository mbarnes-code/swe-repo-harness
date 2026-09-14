"""§15.1 item 3, Wave 7.4 batch 37 — mutation-proof tests for `cli.py`'s `TransformPipelineWorker`
and its Phase 2 (§3.2) support classes (group G5), scoped by
`.superpowers/sdd/round-VIII-qa-qc/worker-mutation-batch37-brief.md`:

    `TransformPipelineWorker` (the class itself), `TransformInput`/`TransformOutput`,
    `_transform_units`, `_TransformPlan`, `_TransformEvidence`, `_ScopedWaveStore`,
    `_TransformClaimHook`, `_TransformSink` (minus its already-proven `__call__` resolution-step
    clause — `tests/test_d89_phase2_claim_lifecycle.py`'s "3. _TransformSink.__call__'s new
    happy-path resolution" section owns that).

Each test below targets the most load-bearing branch of its symbol that the existing reaching
tests (`tests/test_transform_e2e.py`, `tests/test_workers_transform.py`, `tests/test_cli.py`,
`tests/test_d89_phase2_claim_lifecycle.py`) leave unexercised — see each test's docstring for the
specific gap and the mutation that was run against it (Rule 12: backup / edit / diff-verify /
test / restore / re-verify-identical, done by hand for each function below and recorded in
`.superpowers/sdd/round-VIII-qa-qc/worker-mutation-batch37-report.md`).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from fleet.cli import (
    RELOCATE_UNIT,
    REWRITE_UNIT,
    TransformInput,
    TransformOutput,
    TransformPipelineWorker,
    _ScopedWaveStore,
    _transform_units,
    _TransformClaimHook,
    _TransformEvidence,
    _TransformPlan,
)
from fleet.models.enums import FailureClass, Phase
from fleet.workers.base import WorkerResult
from fleet.workers.rewrite import AnchoredRejection, FailedApproach, GuardOffEvent

# Reuse the existing D89 Phase 2 wiring rather than re-deriving it: `wired`, `_sink`, `_payload`,
# `_init_transform_worktree` and `_task_row` are the real `StateWriter`/`SqliteStateRepository`
# scaffolding `tests/test_d89_phase2_claim_lifecycle.py` already built and validated for this
# exact subsystem. Importing them (read-only; this file never edits that module) keeps the new
# tests here consistent with the ones that already exercise `_TransformSink`/`_TransformClaimHook`
# instead of inventing a second, subtly different fixture.
from tests.test_d89_phase2_claim_lifecycle import (  # noqa: F401 - fixtures used by name
    NOW,
    OWNER,
    REPO,
    RUN,
    _clean_write_slot,
    _init_transform_worktree,
    _payload,
    _sink,
    _task_row,
    _Wired,
    db_path,
    wired,
)

# =======================================================================================
# 1. TransformPipelineWorker.run() — the checkpoint-rejected re-entry reset (§7.1)
# =======================================================================================
#
# Gap: no existing test drives `TransformPipelineWorker.run()` with fake step workers, so the
# branch at cli.py ~5570-5578 — where a step's `preconditions_hold` returns False on a resumed
# dispatch (`payload.remaining_units is not None`) and `owed` is reset to the FULL unit set — is
# never exercised in isolation. `tests/test_transform_e2e.py` drives the real workers end to end
# and never forces a checkpoint mismatch. This is the exact defect §7.1 names: without the reset,
# a step whose on-disk state does not match the checkpoint would be silently treated as already
# done (`java/java/com/x`-shaped double-application risk, generalized to "skip work that was
# never actually completed").


class _FakeStep:
    """Records what it was called with; returns pre-programmed `preconditions_hold`/`run` values.

    `TransformPipelineWorker.__slots__ = ("_workers",)` and `_workers` is a plain instance
    attribute (not const-protected), so a freshly constructed worker's dict of real steps can be
    swapped for fakes after `__init__` runs — no monkeypatching of `get_worker` needed.
    """

    def __init__(self, *, precondition_result: bool, run_result: WorkerResult[Any]) -> None:
        self._precondition_result = precondition_result
        self._run_result = run_result
        self.precondition_calls: list[Any] = []
        self.run_calls: list[Any] = []

    async def preconditions_hold(self, ctx: object, payload: object) -> bool:
        self.precondition_calls.append(payload)
        return self._precondition_result

    async def run(self, ctx: object, payload: object) -> WorkerResult[Any]:
        self.run_calls.append(payload)
        return self._run_result


class _FakeLog:
    def info(self, *args: object, **kwargs: object) -> None:
        pass


class _FakeCtx:
    """Only the attributes `TransformPipelineWorker.run()` itself reads; the two fake steps never
    touch `ctx` at all, so nothing heavier (a real `WorkerContext`) is needed."""

    attempt = 1
    tier = "DETERMINISTIC"
    context_policy = None
    log = _FakeLog()
    workdir = "/nonexistent"


def _transform_payload(**overrides: object) -> TransformInput:
    base: dict[str, object] = {
        "repo_id": "acme",
        "branch": "migrate/acme",
        "phase_pre_commit_sha": "a" * 40,
        "dest_path": "dest",
        "sources": ("a.py",),
        "targets": ("b.py",),
    }
    base.update(overrides)
    return TransformInput.model_validate(base)


async def test_run_resets_owed_to_the_full_unit_set_when_the_checkpoint_no_longer_holds() -> None:
    """A resumed dispatch (`remaining_units=("rewrite:b.py",)`, i.e. relocation already landed)
    whose relocate step reports its precondition does NOT hold must re-run relocate from
    scratch — `completed_units=[]` on the RelocateInput it is handed, not `["a.py"]` — and the
    dispatch's own `completed_units` must not claim `relocate:a.py` as already landed.

    Confirmed load-bearing: removing the `owed = set(units)` reset (leaving `owed` at the resumed,
    narrower set) makes `_relocate_input` compute `completed_units=["a.py"]` (since
    `relocate:a.py` is absent from the narrow `owed`) and the dispatch report
    `completed_units=["relocate:a.py"]` even though the step's own precondition said the on-disk
    state does not match — exactly the silent-skip defect §7.1 exists to forbid.
    """
    payload = _transform_payload(remaining_units=("rewrite:b.py",))
    relocate = _FakeStep(
        precondition_result=False,
        run_result=WorkerResult(status="ok", output=None, completed_units=[]),
    )
    rewrite = _FakeStep(
        precondition_result=True,
        run_result=WorkerResult(status="ok", output=None, completed_units=[]),
    )
    worker = TransformPipelineWorker()
    worker._workers = {"relocate": relocate, "rewrite": rewrite}  # type: ignore[dict-item]

    result = await worker.run(_FakeCtx(), payload)  # type: ignore[arg-type]

    assert relocate.run_calls[0].completed_units == [], (
        "relocate's own precondition failed, so it must be re-handed EVERY source as not-yet-"
        "completed, never the resumed dispatch's narrower 'already done' view"
    )
    assert result.completed_units == [], (
        "nothing may be reported landed until the steps actually ran under the reset owed set"
    )


async def test_preconditions_hold_requires_both_a_checkpoint_and_a_live_worktree(
    tmp_path: Path,
) -> None:
    """`preconditions_hold` is `False` whenever `remaining_units is None` (a fresh, non-resumed
    dispatch has nothing to re-enter) and `False` when `remaining_units` names a checkpoint but
    the worktree directory is gone — both are one-line branches with no existing direct test.
    """
    worker = TransformPipelineWorker()
    fresh = _transform_payload()
    resumed = _transform_payload(remaining_units=("rewrite:b.py",))

    class _Ctx:
        workdir = str(tmp_path / "does-not-exist")

    assert await worker.preconditions_hold(_Ctx(), fresh) is False, (  # type: ignore[arg-type]
        "no checkpoint (remaining_units is None) must never be treated as re-entrant"
    )
    assert await worker.preconditions_hold(_Ctx(), resumed) is False, (  # type: ignore[arg-type]
        "a checkpoint naming a worktree that is no longer on disk must not be admitted"
    )

    class _CtxReal:
        workdir = str(tmp_path)

    ctx_real: Any = _CtxReal()
    assert await worker.preconditions_hold(ctx_real, resumed) is True
    assert await worker.preconditions_hold(ctx_real, fresh) is False, (
        "a real, on-disk worktree must NOT be enough on its own — remaining_units is None means "
        "there is no checkpoint to re-enter regardless of what is on disk"
    )


# =======================================================================================
# 2. TransformInput / TransformOutput — the sha pattern that gates every downstream git read
# =======================================================================================
#
# Gap: `TransformInput` is constructed directly in only two other test files
# (`tests/test_d89_phase2_claim_lifecycle.py`, `tests/test_d89_phase2_reconciliation.py`), and
# both always pass a syntactically valid 40-hex `phase_pre_commit_sha`. No test proves the
# `Field(pattern=...)` constraint actually rejects a malformed one — a real risk, since a bad
# anchor is used verbatim in git range queries downstream (`_transform_units`'s docstring notes
# the anchor is a "fact about git the worker must be handed", not re-derived).


def test_transform_input_rejects_a_pre_commit_sha_that_is_not_forty_lowercase_hex_chars() -> None:
    valid = _transform_payload(phase_pre_commit_sha="a" * 40)
    assert valid.phase_pre_commit_sha == "a" * 40

    with pytest.raises(ValidationError):
        _transform_payload(phase_pre_commit_sha="abc123")  # valid hex, wrong length
    with pytest.raises(ValidationError):
        _transform_payload(phase_pre_commit_sha="g" * 40)  # right length, non-hex


def test_transform_output_context_policy_defaults_to_none_matching_the_deterministic_rung() -> None:
    """§6's CHECK ties `(tier = 'DETERMINISTIC') = (context_policy IS NULL)` — this model's
    default values must already satisfy that pairing for a bare `TransformOutput(repo_id=...)`,
    the shape every `_TransformSink` test in this file and `test_d89_phase2_claim_lifecycle.py`
    constructs. No existing test asserts the pairing directly; it is only ever satisfied
    incidentally."""
    output = TransformOutput(repo_id="acme")
    from fleet.models.enums import TransformTier

    assert output.tier == str(TransformTier.DETERMINISTIC)
    assert output.context_policy is None


# =======================================================================================
# 3. _transform_units — the ordering the checkpoint's `completed_units` list depends on
# =======================================================================================
#
# Gap: never called directly by any existing test; only exercised transitively inside
# `TransformPipelineWorker.run()` via the real end-to-end driver, which never asserts the raw
# unit-id list's shape.


def test_transform_units_lists_every_relocate_unit_before_any_rewrite_unit_with_its_namespace() -> (
    None
):
    payload = _transform_payload(sources=("s1.py", "s2.py"), targets=("t1.py",))

    units = _transform_units(payload)

    assert units == [
        f"{RELOCATE_UNIT}s1.py",
        f"{RELOCATE_UNIT}s2.py",
        f"{REWRITE_UNIT}t1.py",
    ], "relocate units must precede rewrite units, each under its own namespace prefix"


# =======================================================================================
# 4. _TransformPlan — frozen so no step can mutate a fact about git after it was computed
# =======================================================================================
#
# Gap: constructed only inside `tests/test_cli.py`'s `_transform_criterion` tests, which read its
# fields but never attempt to write one. `frozen=True` is exactly what the class's own docstring
# claims matters ("every field is a fact about git the worker must be handed rather than
# re-derive") — a dataclass that silently lost `frozen=True` would still work for every existing
# test, since none of them ever assigns to a field.


def test_transform_plan_is_frozen_and_cannot_be_mutated_after_construction() -> None:
    plan = _TransformPlan(
        repo_id="acme",
        worktree=Path("/work/acme"),
        branch="migrate/acme",
        dest_path="dest",
        import_specifier="com.acme",
        pre_commit_sha="a" * 40,
        base_ref="main",
        sources=("s1.py",),
        targets=("t1.py",),
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.dest_path = "elsewhere"  # type: ignore[misc]


# =======================================================================================
# 5. _TransformEvidence.record() — the cross-attempt MERGE branch (`prior is not None`)
# =======================================================================================
#
# Gap: `tests/test_transform_e2e.py`'s two `_TransformEvidence` tests both exercise the
# `prior is not None` branch, but neither ever passes a non-empty `commits` or `skipped` list on
# the SECOND `record()` call for the same repo — so `prior.commits.extend(output.commits)` and
# `prior.skipped.extend(output.skipped)` are reached but their effect is never asserted (extending
# with `[]` is indistinguishable from not extending at all).


def test_transform_evidence_record_merges_commits_and_skipped_across_attempts() -> None:
    evidence = _TransformEvidence()

    evidence.record(TransformOutput(repo_id="repo1", commits=["c1"], skipped=["u1"]))
    evidence.record(TransformOutput(repo_id="repo1", commits=["c2"], skipped=["u2"]))

    stored = evidence.by_repo["repo1"]
    assert stored.commits == ["c1", "c2"], "a later attempt's commits must be appended, not lost"
    assert stored.skipped == ["u1", "u2"], "a later attempt's skips must be appended, not lost"


# =======================================================================================
# 6. _ScopedWaveStore — the membership narrowing that makes `--repo` honest
# =======================================================================================
#
# Gap: `_ScopedWaveStore` is never instantiated by any test directly (confirmed by grep across
# `tests/`) — only mentioned in prose comments about a *different* class's `append_blocked_by`.
# Its entire reason to exist (per its own docstring) is the `wave_members` narrowing; that
# behaviour has zero direct coverage.


class _FakeInnerStore:
    def __init__(self, members: tuple[str, ...]) -> None:
        self._members = members

    async def wave_members(self, run_id: str, wave_index: int) -> tuple[str, ...]:
        return self._members


async def test_scoped_wave_store_narrows_to_only_when_given_and_passes_through_when_none() -> None:
    inner = _FakeInnerStore(("repoA", "repoB", "repoC"))

    unscoped = _ScopedWaveStore(inner, None)  # type: ignore[arg-type]
    scoped = _ScopedWaveStore(inner, ["repoA", "repoC"])  # type: ignore[arg-type]

    assert await unscoped.wave_members("run1", 0) == ("repoA", "repoB", "repoC"), (
        "only=None must pass every member through unfiltered"
    )
    assert await scoped.wave_members("run1", 0) == ("repoA", "repoC"), (
        "only=[...] must exclude a member outside the scope, e.g. a --repo filter"
    )


# =======================================================================================
# 7. _TransformClaimHook — lease_ttl_s is THIS hook's own field, not a hardcoded constant
# =======================================================================================
#
# Gap: `tests/test_d89_phase2_claim_lifecycle.py`'s three `_TransformClaimHook` tests all assert
# `lease_expires_at is not None`, which a hook that hardcoded some OTHER TTL value would still
# satisfy. None of them checks the lease actually expires `lease_ttl_s` seconds after `clock()` —
# the one value that makes `lease_ttl_s` a real constructor parameter rather than a name nobody
# reads back.


async def test_claim_hook_computes_lease_expires_at_from_its_own_configured_ttl(
    wired: _Wired,  # noqa: F811 - imported fixture, used by name per pytest convention
    tmp_path: Path,
) -> None:
    _writer, read_conn, repo = wired
    work_dir = _init_transform_worktree(tmp_path / "work")
    ttl_s = 4321
    hook = _TransformClaimHook(
        repository=repo,
        read_conn=read_conn,
        run_id=RUN,
        owner=OWNER,
        clock=lambda: NOW,
        lease_ttl_s=ttl_s,
        work_dir=work_dir,
    )

    await hook(repo_id=REPO, phase=Phase.TRANSFORM, payload=_payload())

    _, _, _, lease_expires_at, _, _ = await _task_row(read_conn)
    assert lease_expires_at is not None
    assert datetime.fromisoformat(lease_expires_at) == NOW + timedelta(seconds=ttl_s), (
        "the claimed lease must expire exactly lease_ttl_s seconds after the hook's own clock(), "
        "not some other hardcoded duration"
    )


# =======================================================================================
# 8. _TransformSink.__call__ — the three ADR-0021 evidence loops (excluding the already-proven
#    task-resolution clause)
# =======================================================================================
#
# Gap: `tests/test_d89_phase2_claim_lifecycle.py`'s `_dispatch` helper always constructs a
# `TransformOutput` with empty `anchored_rejections`/`failed_approaches`/`guard_off`, so none of
# the three per-event loops in `_TransformSink.__call__` (cli.py ~6102-6149: one `attempts` row
# per anchored rejection, one `rejected_approaches` row per failed approach, one `findings` row
# per guard-off event) is exercised by ANY existing test in the repo — confirmed by grepping for
# `anchored_rejections`/`guard_off`/`failed_approaches` outside `src/`: the only other hits are
# `RewriteWorker`-level tests proving the WORKER populates these lists, never that the SINK
# persists them.


async def test_sink_writes_one_row_per_anchored_rejection_failed_approach_and_guard_off_event(
    wired: _Wired,  # noqa: F811 - imported fixture, used by name per pytest convention
) -> None:
    writer, read_conn, repo = wired
    sink = _sink(writer, repo, read_conn)

    rejection = AnchoredRejection(
        unit=f"{REWRITE_UNIT}dest/A.java",
        approach_signature="a" * 64,
        reason="re-fingerprints a previously rejected approach",
    )
    failed = FailedApproach(
        unit=f"{REWRITE_UNIT}dest/A.java",
        approach_signature="b" * 64,
        reason="check_diff rejected the patch",
        failure_class=FailureClass.PATCH_REJECTED,
    )
    guard_off = GuardOffEvent(unit=f"{REWRITE_UNIT}dest/A.java", approach_signature="c" * 64)

    await sink(
        repo_id=REPO,
        phase=Phase.TRANSFORM,
        fence=1,
        result=WorkerResult[TransformOutput](
            status="ok",
            output=TransformOutput(
                repo_id=REPO,
                anchored_rejections=[rejection],
                failed_approaches=[failed],
                guard_off=[guard_off],
            ),
        ),
    )

    async with read_conn.execute(
        "SELECT exit_code, command, approach_signature FROM attempts "
        " WHERE run_id = ? AND repo_id = ? AND phase = ? AND failure_class = 'ANCHORED_REPEAT'",
        (RUN, REPO, int(Phase.TRANSFORM)),
    ) as cursor:
        rejection_rows = await cursor.fetchall()
    assert len(rejection_rows) == 1, "one attempts row per anchored rejection"
    assert rejection_rows[0][0] is None, "ANCHORED_REPEAT executed nothing: exit_code IS NULL"
    assert rejection_rows[0][1] == "[]"
    assert rejection_rows[0][2] == "a" * 64

    async with read_conn.execute(
        "SELECT approach_signature, reason, failure_class FROM rejected_approaches "
        " WHERE run_id = ?",
        (RUN,),
    ) as cursor:
        failed_rows = await cursor.fetchall()
    assert len(failed_rows) == 1, "one rejected_approaches row per genuinely failed approach"
    assert failed_rows[0] == ("b" * 64, failed.reason, str(FailureClass.PATCH_REJECTED))

    async with read_conn.execute(
        "SELECT kind, severity FROM findings WHERE run_id = ? AND repo_id = ?",
        (RUN, REPO),
    ) as cursor:
        finding_rows = await cursor.fetchall()
    assert len(finding_rows) == 1, "one findings row per guard-off event"
    assert finding_rows[0] == ("AnchoringGuardOff", "warn")
