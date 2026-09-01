"""SPEC §12 item 47 — "the registries are stateless, total, and order-independent" (§7.2,
ADR-0020/0023), the three sub-clauses that had no coverage at all:

1. The `workers` registry (`fleet.orchestrator.registry`) has no statelessness sweep — every
   other §7.2 registry (manifests, ecosystems) does, either baked into `discover()` itself or
   exercised per-file by individual worker tests, but nothing walks the REAL registry and checks
   every entry.
2. The `backends` registry (`fleet.llm.client`) registers INSTANCES, not classes, and several of
   those instances legitimately hold a constructor-injected collaborator (`_transport`, `_env` —
   CLAUDE.md guardrail 3's dependency inversion). A bare `vars(inst) == {}` check — the shape that
   works for `ManifestAdapter`/`EcosystemAdapter` — would misfire on those, so this file checks
   the property that actually matters for an instance-registering registry: the registered
   object is a genuine singleton (re-discovery returns the SAME object) and no per-call attribute
   is added or replaced as a side effect of answering for two different targets.
3. `preconditions_hold` being `abstract` on `BaseWorker` was asserted only via "every worker
   shipped today happens to override it" (`tests/test_workers_base.py`), which cannot fail if the
   `@abstractmethod` decorator itself were deleted and the base defaulted to `return True` — no
   worker in the tree would need to change for that mutation to go unnoticed. This file adds the
   structural proof: a probe subclass that overrides everything BUT `preconditions_hold` must be
   un-instantiable, independent of what any shipped worker does.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from fleet.llm import client as client_module
from fleet.models.enums import Phase
from fleet.models.tasks import BackendTarget, ModelCapabilities
from fleet.orchestrator import registry
from fleet.workers.base import (
    BaseWorker,
    WorkerContext,
    WorkerInput,
    WorkerOutput,
    WorkerResult,
    assert_stateless,
    implements_preconditions,
)

# =======================================================================================
# 1. the workers registry — real `discover()`, not a local re-scan
# =======================================================================================


def test_every_registered_worker_is_stateless() -> None:
    """Mirrors `tests/test_manifests.py::test_every_registered_adapter_is_stateless`, but for the
    workers registry, which had zero `assert_stateless` call sites against its own `discover()`
    (individual worker test files check ONE worker instance each; nothing swept the whole live
    registry). The runner instantiates every worker with `get_worker(name)()` — no arguments —
    so a fresh no-arg instance is exactly what production constructs.
    """
    workers = registry.discover()
    assert workers, "the workers registry discovered nothing"
    for name, cls in workers.items():
        try:
            assert_stateless(cls())
        except TypeError as exc:
            raise AssertionError(f"{name} ({cls.__qualname__}): {exc}") from exc


# =======================================================================================
# 2. the backends registry — instances, not classes; a shape-appropriate check
# =======================================================================================


def _target_for(backend_name: str, *, suffix: str) -> BackendTarget:
    """A minimal but VALIDATING target for `backend_name`, distinct per `suffix` so two calls are
    genuinely asking about two different things — not two copies of one call."""
    fields: dict[str, Any] = {
        "backend": backend_name,
        "model_id": f"probe-{suffix}",
        "price": "free",
    }
    if backend_name in ("bedrock", "vertex"):
        fields["region"] = f"region-{suffix}"
    if backend_name == "anthropic":
        fields["api_key_env"] = f"PROBE_KEY_{suffix.upper()}"
    return BackendTarget(**fields)


def _assert_backend_instance_is_stateless_across_calls(name: str, instance: Any) -> None:
    """What "stateless" means for a registry that holds INSTANCES rather than classes.

    `vars(inst) == {}` — the check that works for `ManifestAdapter`/`EcosystemAdapter` — is the
    wrong shape here: `OpenAICompatibleBackend`/`VertexBackend`/`BedrockBackend` legitimately
    carry a constructor-injected `_transport`/`_env` (dependency inversion, CLAUDE.md guardrail
    3), fixed once at construction and never touched again. What must NOT happen is a call
    picking up a NEW attribute, or replacing an EXISTING one, as a side effect of being asked
    about two different targets — the same cross-repo-leak concern §7.2 states for adapters,
    applied here to two different calls sharing one singleton instead of two different repos.
    """
    before = dict(vars(instance))
    instance.declared_capabilities(_target_for(name, suffix="one"))
    instance.declared_capabilities(_target_for(name, suffix="two"))
    after = dict(vars(instance))
    leaked = set(after) - set(before)
    replaced = {k for k in before if after.get(k) is not before[k]}
    if leaked or replaced:
        raise TypeError(
            f"{type(instance).__qualname__} ({name!r}) leaked call state: "
            f"new attributes {sorted(leaked)}, replaced attributes {sorted(replaced)}"
        )


def test_every_registered_backend_is_stateless_across_calls() -> None:
    """Registration is a ONE-TIME construction (`register_backend`'s `cls()`, run once at import
    time) — re-discovery must return the SAME object, and no per-call attribute may appear or
    change identity across two calls answering about two different targets.
    """
    first = client_module.discover()
    second = client_module.discover()
    assert first, "the backends registry discovered nothing on this host"
    for name, instance in first.items():
        assert second[name] is instance, f"{name}: discover() re-constructed a registered backend"
        _assert_backend_instance_is_stateless_across_calls(name, instance)


def test_backend_statelessness_check_rejects_a_leaking_backend() -> None:
    """The check above must actually fire, or it is decoration — same reasoning as
    `tests/test_manifests.py::test_discover_rejects_a_stateful_adapter`. A decoy class, never
    registered in the live `_BACKENDS` dict, so this cannot perturb any other test's registry
    state."""

    class _LeakyBackend:
        name: ClassVar[str] = "leaky_decoy_for_statelessness_test"
        version: ClassVar[int] = 1

        def declared_capabilities(self, target: BackendTarget) -> ModelCapabilities:
            self.last_model_id = target.model_id  # the exact defect: per-call state on self
            return ModelCapabilities()

        async def invoke(self, *args: object, **kwargs: object) -> object:  # pragma: no cover
            raise NotImplementedError

    leaky = _LeakyBackend()
    with pytest.raises(TypeError, match="leaked call state"):
        _assert_backend_instance_is_stateless_across_calls(_LeakyBackend.name, leaky)


# =======================================================================================
# 3. `preconditions_hold` is abstract on `BaseWorker`, structurally — not by convention
# =======================================================================================


def test_preconditions_hold_is_abstract_on_baseworker() -> None:
    """§7.1 constraint 7. A probe subclass that overrides EVERYTHING BaseWorker requires except
    `preconditions_hold` must be un-instantiable — proving the abstractness itself is what blocks
    silent inheritance, independent of whether any worker shipped today happens to override it
    (they all do; see the registry sweep below, which is the complementary — and insufficient
    alone — half of this proof: a mutation deleting `@abstractmethod` and defaulting to `return
    True` changes nothing about what any shipped worker overrides, so that sweep alone cannot
    catch it).
    """
    assert getattr(BaseWorker.preconditions_hold, "__isabstractmethod__", False), (
        "preconditions_hold must be declared @abstractmethod on BaseWorker"
    )

    class _NoPreconditionsWorker(BaseWorker[WorkerInput, WorkerOutput]):
        name: ClassVar[str] = "probe-no-preconditions"
        phase: ClassVar[Phase] = Phase.TRANSFORM
        input_model: ClassVar[type[WorkerInput]] = WorkerInput
        output_model: ClassVar[type[WorkerOutput]] = WorkerOutput

        async def run(self, ctx: WorkerContext, payload: WorkerInput) -> WorkerResult[WorkerOutput]:
            raise NotImplementedError  # pragma: no cover - construction must fail first

    with pytest.raises(TypeError, match="preconditions_hold"):
        _NoPreconditionsWorker()  # type: ignore[abstract]


def test_every_registered_worker_overrides_preconditions_hold() -> None:
    """The registry-wide sweep — walks the REAL `fleet.orchestrator.registry.discover()`, not a
    local `pkgutil` re-scan, so it cannot silently drift from what the runner actually dispatches
    to. Complementary to the structural proof above: this one shows every SHIPPED worker claims
    its own re-entry contract; the one above shows that claim is enforced by construction and not
    by convention.
    """
    workers = registry.discover()
    assert workers, "the workers registry discovered nothing"
    offenders = [name for name, cls in workers.items() if not implements_preconditions(cls)]
    assert offenders == [], f"these registered workers inherit re-entry silently: {offenders}"
