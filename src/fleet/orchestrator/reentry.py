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
2. **`DEGRADED` is a hard stop, never a phase to demote or to search past (ADR-0077 §5).** A
   `DEGRADED` phase leaves the machine only through a budgeted stub-revalidation round; routing it
   back to `PENDING` here would spend that budget through a side door, with no round recorded. So
   a `DEGRADED` row counts as settled when locating the frontier, and if the backward walk reaches
   one, it stops there without moving the floor onto it.

Terminal rows (`REQUIRES_HUMAN_INTERVENTION`) and repos with nothing left unsettled both mean
"nothing for this function to compute": `None`.
"""

from __future__ import annotations

from collections.abc import Mapping

from fleet.models.enums import Phase, RepoStatus
from fleet.state.repository import PhaseRow

_SETTLED_FOR_DEMOTION: frozenset[RepoStatus] = frozenset(
    {RepoStatus.SUCCEEDED, RepoStatus.SKIPPED, RepoStatus.DEGRADED}
)
# `DEGRADED` is included here per ADR-0077 §5 — not because it is settled in the ordinary sense
# (`enums.TERMINAL_STATUSES` deliberately excludes it, since it is resolvable via revalidation),
# but because this function must never pick it as the frontier to re-enter or as a phase to demote.


def _status_of(row: PhaseRow | None) -> RepoStatus:
    """A missing `phases` row is the schema default: `PENDING` (brief, acceptance criterion 3)."""
    return RepoStatus.PENDING if row is None else row.status


def phase_floor(
    rows: Mapping[Phase, PhaseRow | None], evidence: Mapping[Phase, bool]
) -> Phase | None:
    """Return the earliest phase this repo must re-enter at, or `None` if nothing should move.

    `rows` should carry an entry for every `Phase`; a phase with no persisted row yet may be
    omitted or mapped to `None` — both mean `PENDING`. `evidence` supplies
    `evidence_holds(repo, phase)` for phases below the frontier; a phase absent from `evidence` is
    treated as not holding (the conservative default: search further back rather than stop early).

    Returns `None` when there is nothing to compute: any phase is
    `REQUIRES_HUMAN_INTERVENTION` (mechanically terminal — resume never touches it), or every
    phase has already settled (nothing left to re-enter). Otherwise returns the `Phase` re-entry
    should start at, which may equal the frontier itself (no backward demotion needed) or an
    earlier phase (the backward search found unmet evidence).
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
        if _status_of(rows.get(phase)) is RepoStatus.DEGRADED:
            break  # ADR-0077 §5: never demoted, and the search stops rather than passing it
        if evidence.get(phase, False):
            break  # evidence holds here -- everything earlier is covered by this phase
        floor = phase
    return floor
