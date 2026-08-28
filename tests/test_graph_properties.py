"""Property-based tests for `src/fleet/graph/sequence.py`'s wave-ordering invariant.

ADR-0013 (`docs/DECISIONS.md`) decided `hypothesis` would property-check "the emitted wave order
never places a repo before a repo it depends on" over random DAGs. That decision was never
implemented — a 2026-08-27 audit found `hypothesis` declared twice in `pyproject.toml` and
imported by zero files. `tests/test_graph_sequence.py` covers the invariant only with hand-picked
fixed graphs (e.g. the three-node chain in
`test_a_dependency_migrates_in_an_earlier_wave_than_its_dependent`). This file is the property
test ADR-0013 called for.

**The relation is strict, not `<=`.** `layer()` in `src/fleet/graph/sequence.py` computes
`wave_index(n) = 0` if `n` has no internal dependencies, else `1 + max(wave_index(deps))`. For any
direct edge `src -> dst` (`src` requires `dst`, §3.1 step 5's fixed orientation), `dst` is one of
`src`'s `deps`, so `waves[src] = 1 + max(...) >= 1 + waves[dst] > waves[dst]`. The existing fixed
test asserts exactly this: `waves["acme-c"] < waves["acme-b"] < waves["acme-a"]` on the chain
`acme-a -> acme-b -> acme-c`, never `<=`. This file derives the same strict inequality from the
random DAGs the strategy below generates, rather than assuming it.

**Acyclicity is guaranteed by construction, not by filtering.** `_random_dag` draws a random
permutation of the node ids and only allows an edge `(src, dst)` where `dst` ranks strictly before
`src` in that permutation. A "dependent requires only things ranked earlier" graph cannot contain a
cycle: if it did, following it around would produce a strictly-decreasing sequence of ranks that
returns to its own start. No `assume()` filter is needed, so hypothesis's health checks (which flag
strategies that reject too many drawn examples) never fire.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from fleet.graph.build import build_graph
from fleet.graph.cycles import break_cycles
from fleet.graph.sequence import assign_waves
from tests.test_graph_cycles import edge, nodes

MAX_NODES = 10
"""Bounds graph size so the suite (already ~14 minutes) stays fast; §3.1's invariant does not
depend on scale, and a random DAG this size still exercises multi-level chains, diamonds, and
disconnected components."""


def _repo_id(i: int) -> str:
    return f"repo-{i}"


@st.composite
def _random_dag(draw: st.DrawFn) -> tuple[list[str], list[tuple[str, str]]]:
    """Draw `(repo_ids, edges)` for a random DAG, `edges` as `(src, dst)` meaning `src` requires
    `dst` — the same orientation `edge()` in `tests/test_graph_cycles.py` encodes.

    Acyclicity by construction: draw a random permutation of `repo_ids` fixing a rank per node,
    then only offer `(src, dst)` pairs where `dst` ranks before `src`. A dependent can therefore
    only require something earlier in the permutation, which forbids any cycle outright — no
    filtering, no `assume()`, no rejected examples.
    """
    n = draw(st.integers(min_value=1, max_value=MAX_NODES))
    repo_ids = [_repo_id(i) for i in range(n)]
    permutation = draw(st.permutations(repo_ids))
    rank = {repo_id: idx for idx, repo_id in enumerate(permutation)}
    candidates = [
        (src, dst) for src in repo_ids for dst in repo_ids if rank[dst] < rank[src]
    ]
    edges = draw(st.lists(st.sampled_from(candidates), unique=True)) if candidates else []
    return repo_ids, edges


@given(random_dag=_random_dag())
@settings(max_examples=200, deadline=None)
def test_a_dependency_always_lands_in_a_strictly_earlier_wave_than_its_dependent(
    random_dag: tuple[list[str], list[tuple[str, str]]],
) -> None:
    """For every edge `src -> dst` (`src` requires `dst`) in a random DAG, `dst`'s wave index is
    strictly less than `src`'s — the property `test_a_dependency_migrates_in_an_earlier_wave_than_
    its_dependent` asserts on one fixed chain, checked here over hypothesis-generated DAGs up to
    `MAX_NODES` nodes."""
    repo_ids, edge_pairs = random_dag
    graph = build_graph(nodes(*repo_ids), [edge(src, dst) for src, dst in edge_pairs])
    report = break_cycles(graph)
    plan = assign_waves(report)
    waves = plan.repo_wave_index

    for src, dst in edge_pairs:
        assert waves[dst] < waves[src], (
            f"dependency {dst!r} (wave {waves[dst]}) must precede its dependent {src!r} "
            f"(wave {waves[src]}) — got waves[{dst!r}] >= waves[{src!r}]"
        )
