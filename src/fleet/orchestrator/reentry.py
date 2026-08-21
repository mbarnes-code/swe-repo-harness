"""Pure computation of the resume-time re-entry floor for one repo (`fleet resume` §11.5 step 5).

`phase_floor` answers exactly one question: given the four `phases` rows already persisted for a
repo and a resume-owned `evidence` verdict per phase, at which phase (if any) should re-entry
begin? It performs no I/O of its own — the caller reads `phases` into `rows`, produces `evidence`
(subtask 5's `evidence_holds`), and performs any actual demotion write (subtask 6's `demote()`);
this module only decides where.

Two rules make this different from a naive "scan the phases forward and stop at the first one
whose precondition is unmet" walk:

1. **The search runs backward from the settled frontier, never forward, and it never consults
   `BaseWorker.preconditions_hold`.** That predicate is not a "may I skip this phase" signal for
   any of its fifteen implementations — a `False` verdict there means "re-run the phase whole from
   its anchor", never "skip it" — and several implementations answer `True` for repos that have
   done nothing at all (an absent BUILD row reads as "the runner has not admitted this repo yet",
   not as evidence of success). Scanning those verdicts forward would promote a never-cloned repo
   straight to the last phase. So this function reads only status rows and the caller-supplied
   `evidence` mapping: it locates the frontier — the earliest phase that has not yet settled — and
   then walks backward from there, asking only whether `evidence` holds at each earlier phase.
2. **`DEGRADED` and `SKIPPED` are hard stops: never a phase to demote, never a phase to search
   past (ADR-0077 §5).** A `DEGRADED` phase leaves the machine only through a budgeted
   stub-revalidation round; routing it back to `PENDING` here would spend that budget through a
   side door, with no round recorded. A `SKIPPED` phase is a config exclusion, and resume "does
   not re-decide the operator's config" — which this function must honour in both directions: the
   floor may not land *on* a `SKIPPED` phase (that re-runs work the operator excluded), and the
   walk may not continue *below* one. The second half is not a nicety: an excluded phase can never
   produce holding evidence, so a walk that passed over it would demote every repo with an excluded
   middle phase all the way to `SCAN`, on every resume. So both statuses count as settled when
   locating the frontier, and if the backward walk reaches either, it stops there without moving
   the floor onto it.

Terminal rows (`REQUIRES_HUMAN_INTERVENTION`) and repos with nothing left unsettled both mean
"nothing for this function to compute": `None`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from fleet.models.enums import Phase, RepoStatus

if TYPE_CHECKING:  # `PhaseRow` is used only in annotations, which `from __future__` defers.
    from fleet.state.repository import PhaseRow

_SETTLED_FOR_DEMOTION: frozenset[RepoStatus] = frozenset(
    {RepoStatus.SUCCEEDED, RepoStatus.SKIPPED, RepoStatus.DEGRADED}
)
# `DEGRADED` is included here per ADR-0077 §5 — not because it is settled in the ordinary sense
# (`enums.TERMINAL_STATUSES` deliberately excludes it, since it is resolvable via revalidation),
# but because this function must never pick it as the frontier to re-enter or as a phase to demote.

_HARD_STOPS: frozenset[RepoStatus] = frozenset({RepoStatus.DEGRADED, RepoStatus.SKIPPED})
# The two statuses ADR-0077 §5 declares non-demotable for a reason of its own (a budgeted
# revalidation round; the operator's config). The backward walk stops at either without moving the
# floor onto it. `SUCCEEDED` is deliberately absent: it is the one status §5 makes demotable, and
# demoting a span of it is what the walk exists to do.


def _status_of(row: PhaseRow | None) -> RepoStatus:
    """A missing `phases` row is the schema default: `PENDING` (brief, acceptance criterion 3)."""
    return RepoStatus.PENDING if row is None else row.status


def phase_floor(
    rows: Mapping[Phase, PhaseRow | None], evidence: Mapping[Phase, bool]
) -> Phase | None:
    """Return this repo's re-entry floor, or `None` if nothing should move.

    The floor is the phase **above** the **highest** phase below the settled frontier whose
    durable evidence still holds **or** which is a `DEGRADED`/`SKIPPED` hard stop
    (`orchestrator/reentry._HARD_STOPS`, tested *before* evidence, so such a row ends the walk
    whatever holds below it), never that phase itself, and `SCAN` only if there is no such phase
    (`docs/SPEC.md` Constraint 7 / §11.5 step 5, ADR-0076 §1, `models/enums.RESUME_DEMOTE` — one
    wording, bound by `tests/test_floor_rule_statements.py`). Never the *earliest*: wherever two
    phases below the frontier hold, the walk stops above the highest and never reaches the
    earliest, so naming the earliest picks a rung this function never returns.

    `rows` should carry an entry for every `Phase`; a phase with no persisted row yet may be
    omitted or mapped to `None` — both mean `PENDING`. `evidence` supplies
    `evidence_holds(repo, phase)` for phases below the frontier; a phase absent from `evidence` is
    treated as not holding (the conservative default: search further back rather than stop early).

    **`evidence` is READ LAZILY, and a mapping that computes on demand is a supported argument.**
    This function asks `evidence` only for phases **strictly below** the frontier — never at it,
    never above it — and the backward walk stops at the first phase that holds or that is a hard
    stop, so it does not ask about anything below that either. A caller may therefore pass a
    `Mapping` whose lookup performs the work rather than a dict of four pre-computed verdicts, and
    `orchestrator.reentry.resume_floor` is the supplied driver that does exactly that: eager
    evaluation would spend Git reads on phases this walk never consults, and re-deriving the
    frontier in the caller so it could pre-compute only the needed span would put the
    settled-for-demotion classification in two modules with nothing binding them (defect D74's
    shape). The guarantee is not a convention:
    `test_phase_floor_asks_evidence_only_for_phases_strictly_below_the_frontier`
    (`tests/test_reentry_evidence.py`) records every lookup and fails on one above. The "absent
    from `evidence`" sentence above still governs a plain mapping; a computing mapping simply
    never has an absent key.

    Returns `None` when there is nothing to compute: any phase is
    `REQUIRES_HUMAN_INTERVENTION` (mechanically terminal — resume never touches it), or every
    phase has already settled (nothing left to re-enter). Otherwise returns the `Phase` re-entry
    should start at, which may equal the frontier itself (no backward demotion needed) or an
    earlier phase (the backward search found unmet evidence). The backward search stops at a
    `DEGRADED` or `SKIPPED` row without ever moving the floor onto it (ADR-0077 §5).
    """
    for phase in Phase:
        if _status_of(rows.get(phase)) is RepoStatus.REQUIRES_HUMAN_INTERVENTION:
            return None

    frontier: Phase | None = None
    for phase in Phase:
        if _status_of(rows.get(phase)) not in _SETTLED_FOR_DEMOTION:
            frontier = phase
            break
    if frontier is None:
        return None  # every phase already settled -- nothing to demote

    floor = frontier
    for value in range(int(frontier) - 1, 0, -1):
        phase = Phase(value)
        if _status_of(rows.get(phase)) in _HARD_STOPS:
            break  # ADR-0077 §5: never demoted, and the search stops rather than passing it
        if evidence.get(phase, False):
            break  # evidence holds here -- everything earlier is covered by this phase
        floor = phase
    return floor


def demotable_phases(statuses: Mapping[Phase, RepoStatus], floor: Phase) -> tuple[Phase, ...]:
    """Which phases at or above `floor` a §11.5 step-5 demotion would actually write.

    THE membership rule of the step-5 write, in ONE place, called by both routes that need it:
    `state.repository.SqliteStateRepository.demote_to_floor`'s in-transaction unit applies it to
    the rows it read under `BEGIN IMMEDIATE`, and `cli._demote_to_floors`' `--dry-run` preview
    applies it to the rows it read through `mode=ro`. The two routes differ only in *which
    snapshot* they are handed, never in the rule. Written out twice -- once as a preview and once
    as a write, in two files -- they would be defect **D74**'s shape exactly: nothing fails when
    they drift, every test stays green, and the preview quietly becomes a lie about what the real
    run will do.

    `SUCCEEDED` is the whole filter, and it is `models.enums.RESUME_DEMOTE`'s domain restated over
    a span: `demote()` raises on every other status, so `PENDING`, `RUNNING` and `BLOCKED` have no
    landed work to discard, and `DEGRADED`/`SKIPPED` are the two statuses ADR-0077 §5 forbids
    demoting at all. A phase with no persisted row is the schema default `PENDING` and is likewise
    not demotable -- which is why this reads `.get` rather than indexing, and why a caller may
    hand it a mapping covering only the rows that exist.

    Pure, with no I/O, so it is callable from inside a `StateWriter` unit (`state/db.py` forbids
    network, git and LLM work there). It deliberately does **not** answer which `checkpoints` rows
    a demotion sweeps: that span is wider than this set by construction (`demote_to_floor` sweeps
    the whole span minus `DEGRADED`, not only the phases it demoted), and it belongs with the
    write rather than with the preview.
    """
    return tuple(
        phase for phase in Phase if phase >= floor and statuses.get(phase) is RepoStatus.SUCCEEDED
    )


# --------------------------------------------------------------------------------------
# §11.5 step 5 — `evidence_holds`: the four per-phase DURABLE evidence predicates
# --------------------------------------------------------------------------------------
# These imports sit here rather than in the header block above because this section was appended
# to a file a sibling lane was editing in the same round, and one contiguous tail keeps the two
# edits independent. Fold them into the header block on the next pass over this file.
from collections.abc import Awaitable, Callable, Iterator  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Final, cast  # noqa: E402

from fleet.sandbox.worktree import slug  # noqa: E402
from fleet.vcs.git import Git  # noqa: E402


@dataclass(frozen=True, slots=True)
class EvidenceRow:
    """The two `phases` columns step 5 reads, and nothing else (Rule 2).

    Deliberately NOT `state.repository.PhaseRow`: that dataclass is "the subset of `phases` the
    orchestrator reads" and it does **not** carry `post_commit_sha`, which two of the four
    predicates below are defined in terms of. `models.state.PhaseRecord` does carry the column but
    drags the whole Pydantic row in. So step 5 declares the two columns it needs and the caller
    projects its own read onto them — `resume_floor` then feeds the same mapping to `phase_floor`,
    which reads only `.status`, so one read serves both halves of the algorithm.

    **`post_commit_sha` is REQUIRED, deliberately, and it is the only defence against a silent
    fleet-wide demotion.** `PhaseRow` does not carry the column and `state/repository.py` contains
    no reader that returns it (measured: zero occurrences of the name in that module), so the
    obvious projection from the row a caller already holds is `EvidenceRow(status=row.status)`.
    With a default that expression constructs, type-checks, runs, and answers `False` at Phases 2
    and 3 for **every** repo — the walk never stops and the whole fleet is demoted to `SCAN` on
    every resume. No fixture can express that mistake, because a fixture that reaches this class
    has already answered the question. So the signature is the mechanism: omission is a
    `TypeError` at construction, not a wrong answer at scale. Pass `None` explicitly for a phase
    that genuinely has no pointer. The read that already returns the column is step 4's
    `cli._ARBITRATED_TASKS_SQL` (`SELECT … p.base_ref, p.pre_commit_sha, p.post_commit_sha`).
    """

    status: RepoStatus
    post_commit_sha: str | None


@dataclass(frozen=True, slots=True)
class RepoEvidence:
    """Everything `evidence_holds` needs about one repo that is not a `phases` row.

    Two filesystem paths, the migrate branch, the repo's destination inside the monorepo, and one
    durable DB fact. `has_verification_report` is passed in rather than queried here because the
    report is persisted as a `findings` row with `kind = 'VerificationReport'` by `cli.py`
    (`cli._record_verification` / `cli._verifications`, `cli.VERIFICATION_KIND`), and importing
    `fleet.cli` from `fleet.orchestrator` would close an import cycle. The predicate stays a
    function of `phases` + Git + this one supplied bool.
    """

    repo_id: str
    mirror: Path
    worktree: Path
    dest: str
    has_verification_report: bool = False
    branch: str = ""

    def migrate_branch(self) -> str:
        """`migrate/<repo>` unless the caller named a branch.

        Anchored on the CONSTRUCTION, not on a pattern: `cli.py` builds `f"migrate/{repo_id}"` at
        three sites — task reconciliation, the ingest clone, and step 4's
        `_reconcile_tasks_with_git` — and this restates them exactly.
        `models.tasks.PullRequestDraft.branch`'s `^migrate/[a-z0-9._-]+$` is deliberately NOT the
        authority here: a hierarchical repo id
        yields `migrate/acme/widget`, whose `/` that character class rejects. A reader who
        "reconciled" this method to the pattern by slugging the id would break Phase-2 evidence for
        every repo with a `/` in its name, because the branch git actually holds is the unslugged
        one. That tension is `cli.py`'s and pre-exists this method; it is named so it is not
        inherited by accident.
        """
        return self.branch or f"migrate/{self.repo_id}"

    @classmethod
    def for_repo(
        cls,
        repo_id: str,
        *,
        cache_dir: Path,
        work_dir: Path,
        dest: str,
        has_verification_report: bool = False,
    ) -> RepoEvidence:
        """Derive the two paths the way production derives them, so step 5 cannot point at a
        directory no worker ever wrote.

        `cache_dir` is `<root>/<run.cache_dir>` — the **unsuffixed** value, which is what every
        other `cache_dir` in `cli.py` means (`_gc_impl`, and the gazelle / resolve / ingest roots).
        This method appends `MIRROR_CACHE_SUBDIR` itself, so the one place that knows git mirrors
        live one level down is this expression rather than every caller. That is not tidiness: a
        caller that passed the unsuffixed path to a parameter expecting the suffixed one would
        probe a directory no worker ever wrote, `_mirror_is_initialized` would answer `False` for
        every repo, and the whole fleet's floor would be `SCAN` on every resume — silently, with
        no fixture able to express it, because the mistake is on the caller's side of the
        boundary. `test_the_mirror_cache_subdir_is_the_one_cli_writes`
        (`tests/test_reentry_evidence.py`) parses the segment out of `cli._scan_payloads`'s own
        source and checks it against the constant.

        The mirror is then `<slug(repo_id)>.git` — `workers/clone.CloneWorker._mirror_path`
        restated with the same `sandbox.worktree.slug` this imports rather than a second
        slugifier. `work_dir` is `<root>/<run.work_dir>`, and the worktree under it is
        `work_dir / repo_id` — `orchestrator.context.RunContext.worktree` and
        `cli._reconcile_tasks_with_git`'s `work_root / repo_id`.
        """
        return cls(
            repo_id=repo_id,
            mirror=cache_dir / MIRROR_CACHE_SUBDIR / f"{slug(repo_id)}.git",
            worktree=work_dir / repo_id,
            dest=dest,
            has_verification_report=has_verification_report,
        )


#: The directory under `<root>/<run.cache_dir>` that holds the per-repo git mirrors. `cli.py`'s
#: scan-payload prologue is the authority — `cache_dir = str((settings.root /
#: settings.config.run.cache_dir / "git").resolve())` — and `RepoEvidence.for_repo` appends this
#: rather than making every caller remember it.
MIRROR_CACHE_SUBDIR: Final = "git"


#: A `Git` bound to one path. Injected (CLAUDE.md guardrail 3) so a test can supply a recording
#: runner without a subprocess, and so a caller can pass its own deadline down.
GitFactory = Callable[[Path], Git]


def _mirror_is_initialized(mirror: Path) -> bool:
    """`workers/clone._mirror_is_initialized`, restated: a directory git would recognise as a bare
    repository, not merely a directory that exists. Restated rather than imported because
    importing `fleet.workers.clone` would execute its `@register_worker` side effect and pull the
    LLM and sandbox stacks into a module the resume driver wants cheap;
    `tests/test_reentry_evidence.py::test_the_clone_facts_agree_with_the_clone_worker_s_own_helpers`
    calls both over the same fixture matrix so the two cannot silently diverge."""
    return (mirror / "HEAD").is_file() and (mirror / "objects").is_dir()


def _worktree_is_initialized(worktree: Path) -> bool:
    """`workers/clone._worktree_is_initialized`, restated (see `_mirror_is_initialized`): a linked
    worktree carries a `.git` FILE pointing at the mirror's admin directory."""
    return (worktree / ".git").exists()


def _worktree_present(repo: RepoEvidence) -> bool:
    """`workers/buildverify.preconditions_hold`'s SECOND refusal, restated over
    `buildverify.dirs_present`: "a missing worktree means the reaper already removed what the
    checkpoint describes".

    Load-bearing rather than defensive, and it must run **before** any Git read: `Git` binds a
    path and shells out with it as `cwd`, so a `resolve()` against a directory that is not there
    raises `FileNotFoundError` instead of answering. A fresh repo has no worktree, and "a fresh
    repo yields `False` at every phase" (§5 row 5) is only true if the phases that read Git check
    this first — without it, `evidence_holds` raises on exactly the input the criterion names.
    """
    return repo.worktree.is_dir()


def _build_file_present(repo: RepoEvidence) -> bool:
    """`workers/buildverify.preconditions_hold`'s third refusal, restated over the same path
    expression: `<worktree>/<dest>/BUILD.bazel`.

    **On the filesystem, not on the integration ref** — which is where this diverges from
    `design-resume-step5.md` §3's table, and deliberately. That table says "present on the
    integration ref"; the implementation it cites checks the worktree, and the scenario §6 option
    C is written for is a `BUILD.bazel` the *reaper removed from disk*. A ref read cannot see that
    deletion — the blob is still reachable from the commit — so reading the ref would answer
    `True` for exactly the repo step 5 exists to demote.
    """
    return (repo.worktree / repo.dest / "BUILD.bazel").exists()


async def _scan_evidence(
    rows: Mapping[Phase, EvidenceRow | None], repo: RepoEvidence, git: GitFactory
) -> bool:
    """Phase 1: the mirror is a git dir, the worktree is a git worktree, and the worktree resolves
    `HEAD` — `workers/clone.CloneWorker.preconditions_hold`'s three facts, in its cost order."""
    _ = rows
    if not _mirror_is_initialized(repo.mirror):
        return False
    if not _worktree_is_initialized(repo.worktree):
        return False
    return await git(repo.worktree).resolve("HEAD") is not None


async def _transform_evidence(
    rows: Mapping[Phase, EvidenceRow | None], repo: RepoEvidence, git: GitFactory
) -> bool:
    """Phase 2: `migrate/<repo>` exists and `phases(r,2).post_commit_sha` resolves **on it**.

    "On it" is an ancestry question, not a resolvability one, and the difference is the whole
    value of the clause: a SHA that resolves in the repo but is not reachable from the branch is a
    commit `vcs/commits.rollback_phase` or a force-reset already took off `migrate/<repo>`, which
    is precisely the state whose Phase-2 result must not be trusted. §11.5's authority table says
    the column is "a **pointer** into Git … never trusted over Git", so the pointer is checked
    against the branch rather than read as the answer.
    """
    if not _worktree_present(repo):
        return False
    branch = repo.migrate_branch()
    handle = git(repo.worktree)
    if await handle.resolve(branch) is None:
        return False
    row = rows.get(Phase.TRANSFORM)
    sha = None if row is None else row.post_commit_sha
    if not sha:
        return False
    if await handle.resolve(sha) is None:
        return False
    return await handle.is_ancestor(sha, branch)


async def _build_evidence(
    rows: Mapping[Phase, EvidenceRow | None], repo: RepoEvidence, git: GitFactory
) -> bool:
    """Phase 3: `phases(r,3).post_commit_sha` resolves ∧ `<dest>/BUILD.bazel` is present.

    **Resolvability, where Phase 2 checks ancestry — and that asymmetry is design §3's, not an
    oversight.** Row 2 of its table says "resolves *on it*", row 3 says only "resolves", and this
    follows the table. The consequence is real and is recorded rather than silently inherited: a
    `build_sha` that a rollback or force-reset took off `migrate/<repo>` still yields `True` here
    whenever `<dest>/BUILD.bazel` happens to be on disk, and because the walk stops at the
    *highest* holder, `TRANSFORM`'s broken pointer is then never examined at all. Tightening this
    to `is_ancestor` is defensible and is deliberately NOT done in subtask 5: it would be a
    divergence from the design with no implementation behind it to arbitrate, unlike the three
    ADR-0088 records. It belongs in a change that can measure the cost against a real fleet.
    """
    if not _worktree_present(repo):
        return False
    row = rows.get(Phase.BUILD)
    sha = None if row is None else row.post_commit_sha
    if not sha:
        return False
    if await git(repo.worktree).resolve(sha) is None:
        return False
    return _build_file_present(repo)


async def _verify_evidence(
    rows: Mapping[Phase, EvidenceRow | None], repo: RepoEvidence, git: GitFactory
) -> bool:
    """Phase 4: `phases(r,3).status is SUCCEEDED` ∧ a persisted `VerificationReport` for the repo.

    **This is the one predicate that must NOT agree with the implementation it mirrors.**
    `workers/rdepverify.preconditions_hold` answers `True` when the BUILD row is *missing*, and
    says why: "No BUILD row at all is a first admission by the runner, not evidence of a failure".
    That reading is correct for an admission gate and catastrophic for evidence — a missing BUILD
    row is the state of every repo that has never built, and answering `True` here would let the
    backward walk stop at Phase 4 and leave a never-cloned repo's floor there. So a missing row is
    `PENDING` (`_status_of`) and `PENDING is not SUCCEEDED` — `False`. The design table's claim
    that each clause "duplicates a fact … so the two cannot disagree" does not hold for this row,
    and the divergence is the point rather than an oversight.
    """
    _ = git
    # `_status_of` — which is what makes "a missing row is `PENDING`" one rule and not two — is
    # annotated for `PhaseRow` because that is the row shape its own caller had. It reads only
    # `.status`, which `EvidenceRow` supplies; the cast is the same one `resume_floor` documents.
    build = cast("PhaseRow | None", rows.get(Phase.BUILD))
    return _status_of(build) is RepoStatus.SUCCEEDED and repo.has_verification_report


_EvidencePredicate = Callable[
    [Mapping[Phase, "EvidenceRow | None"], RepoEvidence, GitFactory], Awaitable[bool]
]

_PREDICATES: Final[Mapping[Phase, _EvidencePredicate]] = {
    Phase.SCAN: _scan_evidence,
    Phase.TRANSFORM: _transform_evidence,
    Phase.BUILD: _build_evidence,
    Phase.VERIFY: _verify_evidence,
}


async def evidence_holds(
    phase: Phase,
    *,
    rows: Mapping[Phase, EvidenceRow | None],
    repo: RepoEvidence,
    git: GitFactory = Git,
) -> bool:
    """Does this repo's DURABLE evidence for `phase` still hold? (`docs/SPEC.md` Constraint 7.)

    This is **not** `BaseWorker.preconditions_hold` and must never be confused with it. That
    method takes a typed payload and a live `WorkerContext`, exists only inside a dispatch, and
    its `False` means "re-run the phase whole" — never "skip". `evidence_holds` reads `phases` and
    Git only, needs no payload, no `WorkerContext` and no worker instance, and its `False` means
    "the durable artefact this phase claims to have produced is not there any more". Constraint
    7's own wording is "the phase's declared preconditions … **against SQLite**", and this is what
    that means; `preconditions_hold` keeps its single call site in `runner._re_entry`, so the two
    predicates cannot disagree because they are never asked the same question.

    A fresh repo answers `False` at every phase. That is the correct answer and it has **no
    promotion effect**: `phase_floor` only ever asks about phases strictly *below* the settled
    frontier, and a fresh repo's frontier is `SCAN`, so the walk asks nothing at all.

    **A `post_commit_sha` that does not resolve is `False`, never an exception.** §11.5 step 4's
    second selector — reconcile "any `phases` row whose `post_commit_sha` does not resolve on its
    branch" — is NOT implemented, and ADR-0087 §4 discloses it: a phase row with a dangling
    pointer and no `RUNNING` task is never reconciled, so step 4 is a satisfied prerequisite for
    the reconciled class only and the Phase-2 and Phase-3 predicates below will meet unresolvable
    SHAs in production. Both possible mistakes are asymmetric and one is much worse: a false
    `False` costs one repo a re-run of work that had in fact landed, while a false `True` leaves a
    repo's floor above work nothing verified. And a raise is worse than either — it would abort
    the whole resume rather than lower one repo's floor, so a dangling pointer for one repo would
    stop the other 249 being reconciled at all. Hence: resolve, and answer `False` on a miss.

    `git` is a factory rather than a `Git` so each predicate can bind the handle it needs; the
    default is the real class.
    """
    try:
        predicate = _PREDICATES[phase]
    except KeyError as exc:  # a fifth phase must fail loudly, not default to False (Rule 11)
        raise ValueError(f"no step-5 evidence predicate for {phase!r}") from exc
    return await predicate(rows, repo, git)


# --------------------------------------------------------------------------------------
# Feeding `evidence_holds` to `phase_floor` lazily
# --------------------------------------------------------------------------------------


class _EvidenceWanted(Exception):
    """`_LazyEvidence` asks for a phase it has not been given yet.

    Deliberately **not** a `KeyError`: `Mapping.get` catches `KeyError` and returns its default,
    so a `KeyError` here would be silently answered `False` and the laziness would degrade into a
    wrong answer instead of a request.
    """

    def __init__(self, phase: Phase) -> None:
        super().__init__(phase)
        self.phase = phase


class _LazyEvidence(Mapping[Phase, bool]):
    """A `Mapping[Phase, bool]` that raises `_EvidenceWanted` for anything it does not hold."""

    def __init__(self, known: Mapping[Phase, bool]) -> None:
        self._known = known

    def __getitem__(self, phase: Phase) -> bool:
        try:
            return self._known[phase]
        except KeyError:
            raise _EvidenceWanted(phase) from None

    def __contains__(self, phase: object) -> bool:
        """Overridden so a membership test answers instead of raising.

        `Mapping.__contains__` is `try: self[key] / except KeyError: return False`, and
        `_EvidenceWanted` is deliberately not a `KeyError` — so the inherited version would let an
        internal control-flow exception out of `phase in evidence`. Inert today (`phase_floor`
        uses `.get` exclusively), but the next `phase_floor` edit or a subtask-7 caller must not
        have to know that.
        """
        return phase in self._known

    def __iter__(self) -> Iterator[Phase]:
        return iter(self._known)

    def __len__(self) -> int:
        return len(self._known)


async def resume_floor(
    rows: Mapping[Phase, EvidenceRow | None],
    probe: Callable[[Phase], Awaitable[bool]],
) -> tuple[Phase | None, dict[Phase, bool]]:
    """`phase_floor`, driven with evidence computed **on demand**, one phase at a time.

    Returns the floor and the evidence that was actually gathered — the second half so a
    `--dry-run` report can say *why* a repo was demoted without a second pass over Git, and so a
    test can assert which phases were asked about at all.

    **Why a replay loop rather than a lazy `__getitem__` that computes.** `phase_floor` is sync
    and `evidence_holds` is async, so a mapping cannot await inside `__getitem__`. Instead
    `phase_floor` — a pure function with no side effects, which is what makes this sound — is
    called with a mapping that *raises* for an unknown phase; the raise is caught here, that one
    phase is awaited, and the pure function is re-run. It terminates because every iteration adds
    a key and `Phase` has four members, so `phase_floor` runs at most five times.

    The point is the one `phase_floor` documents: evidence is computed only for the phases the
    backward walk actually reaches. Evaluating all four eagerly would spend two `rev-parse`-class
    reads and a tree probe per repo per resume on phases the walk never consults, and re-deriving
    the frontier in the caller to avoid that would put the settled-for-demotion classification in
    two modules with nothing binding them — defect D74's shape.

    **THIS FUNCTION ISOLATES NOTHING, AND THE CALLER OWNS PER-REPO CONTAINMENT.** Every exception
    from `probe` propagates. That is not only the dangling-pointer case ADR-0088 §4 ruled on —
    which is contained *inside* `evidence_holds`, where `Git.resolve` answers `None` for a settled
    "no such rev" — but the case one layer below it: `Git.resolve` and `Git.is_ancestor` both call
    `_require_settled`, which raises `GitCommandError` when a probe never started or was killed at
    its deadline (D42, deliberate and correct there). So a single `rev-parse` that hits the wave
    deadline raises here, and a caller that drives the fleet in one `try` gets exactly the outcome
    ADR-0088 §4 argues against: one repo stops the other 249 being reconciled. Subtask 7 must wrap
    the call **per repo** and record the failure as an unresolved repo, the way
    `cli._reconcile_tasks_with_git` already does for step 4 (`_unresolved`, with the reason
    carried verbatim rather than collapsed to a name — D44). Stated here, and in ADR-0088 §7,
    because an obligation that lives only in a report is not an obligation.
    """
    known: dict[Phase, bool] = {}
    while True:
        wanted: Phase | None = None
        try:
            # `phase_floor` reads only `.status` off each row, which `EvidenceRow` supplies; its
            # annotation names `PhaseRow` because that is what its own caller had. One cast here
            # rather than a second row read in every caller — fold it away by widening
            # `phase_floor`'s annotation to a status-only Protocol once this file is quiet.
            floor = phase_floor(cast("Mapping[Phase, PhaseRow | None]", rows), _LazyEvidence(known))
        except _EvidenceWanted as exc:
            if exc.phase in known:  # pragma: no cover - `_LazyEvidence` returns known phases
                raise RuntimeError(f"step-5 evidence re-requested for {exc.phase!r}") from None
            wanted = exc.phase
        if wanted is None:
            return floor, known
        # Awaited OUTSIDE the `except` block on purpose: a `GitCommandError` raised in here would
        # otherwise surface chained behind "During handling of the above exception", and the first
        # thing an operator would read in the traceback is `_EvidenceWanted` — an internal
        # control-flow signal that has nothing to do with why their resume stopped.
        known[wanted] = await probe(wanted)
