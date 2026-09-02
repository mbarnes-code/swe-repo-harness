"""SPEC §12.42's fixture-backend proof: "a new backend costs one file and one registry line."

SPEC's literal claim (`docs/SPEC.md`, search "A new backend costs one file and one registry
line"): a fixture backend added as `tests/fixtures/llm/echo_backend.py` — one `@register_backend`
class implementing `declared_capabilities` and `invoke` — is selected by a test profile and
serves every role in a full fixture run, with `git diff --stat` over `src/fleet/` showing zero
changed files.

**The registration mechanism, confirmed against source before writing this test** (per this
task's brief): `src/fleet/llm/client.py`'s `discover()` is a `pkgutil` walk of the
`fleet.llm.backends` PACKAGE and never sees a file living at `tests/fixtures/llm/`. The actual
mechanism is plain Python import machinery — `register_backend` is a decorator that runs the
instant its class is defined, writing straight into the module-level `_BACKENDS` dict. So
IMPORTING `tests.fixtures.llm.echo_backend` is itself the registration act; nothing under
`src/fleet/` has to see this fixture backend at all. This test proves that reading, not asserting
it: `client_module.registry()` is checked BEFORE and AFTER the import.

**"A test profile" here is `LlmRouter` built directly**, the same pattern already used by
`tests/test_workers_scan.py`, `tests/test_workers_transform.py`, `tests/test_llm_findings.py` and
`tests/test_run_context_llm_cache.py` to exercise the LLM boundary without a `config/models.yaml`
file on disk (`LlmRouter` is a pure function of an already-parsed role/target mapping — the YAML
parsing is `settings.py`'s separate job, already covered by other tests, and out of this task's
scope). Every `Role` is mapped to its real SPEC tier (`SPEC_ROLE_TIERS`) and every `ModelTier` is
routed to the SAME one-target list naming `backend: echo` — so every role, at every tier, reaches
the fixture backend.

**What the `git status --porcelain -- src/fleet/` assertions prove, and what they do not.** They
prove that THIS run — registering the fixture backend, building a router that sends every role to
it, and driving one `complete()` call per role through it — needed zero `src/fleet/` changes to
exist or to happen: the check runs once before anything below happens and once after everything
below has happened, over the same working tree. They do NOT prove `src/fleet/` is never touched by
anything else, ever; a sibling test or a concurrent process touching `src/fleet/` during this
test's run would also fail this assertion, which is correct — the assertion's job is exactly to
catch any `src/fleet/` write, from any source, occurring around this fixture-backend run.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from pydantic import BaseModel

from fleet.llm import client as client_module
from fleet.llm.client import CallPolicy, LadderModelClient, Message
from fleet.llm.roles import SPEC_ROLE_TIERS, LlmRouter, Role
from fleet.models.enums import ModelTier
from fleet.models.tasks import BackendTarget

REPO_ROOT = Path(__file__).resolve().parents[1]


class EchoAnswer(BaseModel):
    """The response contract this test drives every role's call through. One field is enough to
    prove validation happened; the fixture backend does not vary its answer by role."""

    ok: bool


def _src_fleet_status() -> str:
    """`git status --porcelain -- src/fleet/`, run against THIS file's own worktree root (never
    the primary checkout, even if this test happens to run from inside one — CLAUDE.md's
    worktree-pinning discipline). Empty output means clean."""
    result = subprocess.run(
        ["git", "status", "--porcelain", "--", "src/fleet/"],  # noqa: S607
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def test_fixture_backend_serves_every_role_with_zero_src_fleet_changes() -> None:
    """Drives all 12 `Role`s through a fixture backend that lives entirely outside `src/fleet/`,
    proving SPEC §12.42's literal claim: registering a new backend, routing every role to it, and
    invoking it is possible with zero `src/fleet/` changes — before, during, and after.
    """
    before = _src_fleet_status()
    assert before == "", f"src/fleet/ was already dirty before this test ran anything: {before!r}"

    assert "echo" not in client_module.registry(), (
        "the echo backend must not already be registered — otherwise importing it below proves "
        "nothing about import-time registration"
    )

    import tests.fixtures.llm.echo_backend as echo_backend

    assert "echo" in client_module.registry(), (
        "importing tests.fixtures.llm.echo_backend did not register it — the "
        "@register_backend decorator did not run, or discover() was needed after all"
    )

    try:
        echo_target = BackendTarget(backend="echo", model_id="echo-1", price="free")
        roles = {str(role): tier for role, tier in SPEC_ROLE_TIERS.items()}
        targets: dict[ModelTier, tuple[BackendTarget, ...]] = dict.fromkeys(
            ModelTier, (echo_target,)
        )
        router = LlmRouter(roles, targets, profile="fixture-echo")

        # `backends=None` (the default) is the point: the client falls back to the live global
        # `registry()` snapshot rather than being handed the instance directly, so this exercises
        # the SAME path a real `RunContext` construction takes (`LadderModelClient.__init__`,
        # `src/fleet/llm/client.py`).
        client = LadderModelClient(router, policy=CallPolicy())

        backend = client_module.registry()["echo"]
        assert isinstance(backend, echo_backend.EchoBackend)
        assert backend.calls == []

        for role in Role:
            messages = [Message(role="user", content=f"role={role.value}")]
            response = asyncio.run(client.complete(role.value, messages, EchoAnswer))
            assert response.value == EchoAnswer(ok=True)

        # The direct proof that every role actually reached `invoke()` once, in role-definition
        # order — not merely that no exception was raised while looping over 12 roles.
        assert backend.calls == [f"role={role.value}" for role in Role]
        assert len(backend.calls) == len(Role)
    finally:
        # Same discipline as `test_llm_client.py::test_register_backend_refuses_a_duplicate_name`
        # — a synthetic registry entry a real backend name would never collide with, popped in
        # `finally` regardless of outcome so this test cannot perturb any other test's registry
        # state for the rest of the session.
        client_module._BACKENDS.pop("echo", None)

    after = _src_fleet_status()
    assert after == "", (
        f"driving the fixture backend through every role left src/fleet/ dirty: {after!r}"
    )
