"""SPEC §12 item 47 — "the registries are stateless, total, and order-independent" (§7.2,
ADR-0020/0023), the three sub-clauses that had no coverage at all:

1. The `workers` registry (`fleet.orchestrator.registry`) has no statelessness sweep — every
   other §7.2 registry (manifests, ecosystems) does, either baked into `discover()` itself or
   exercised per-file by individual worker tests, but nothing walks the REAL registry and checks
   every entry.
2. The `backends` registry (`fleet.llm.client`) registers INSTANCES, not classes. Round T
   substituted a weaker property (singleton identity + no new/replaced attribute across two
   `declared_capabilities()` calls) for the criterion's literal `vars(inst) == {}`, because at the
   time `OpenAICompatibleBackend`/`BedrockBackend`/`VertexBackend` legitimately stored a
   constructor-injected `_transport`/`_env` on `self` even in the default (registered) case. That
   was reverted (see `docs/CRITERIA_PLAN.md` §47) and fixed in production: `register_backend`'s
   `cls()` — zero arguments — now leaves every backend's `__dict__` empty; the collaborators
   still exist for test injection, but are only stored on `self` when a test explicitly passes
   one, and such an instance is never the registry's singleton. This file now checks the literal
   `vars(inst) == {}`, exactly as `test_manifests.py`/`test_every_registered_worker_is_stateless`
   do for their own registries, plus the stronger cross-call form the round-T test already
   proved worth keeping: two `declared_capabilities()` calls for two different targets must not
   leave the registered singleton's `__dict__` non-empty either.
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
    if backend_name == "harmony_gpt_oss":
        # `declared_capabilities` validates `base_url` first (§13 row 36) and raises without it.
        fields["base_url"] = f"http://probe-{suffix}.invalid/v1"
    return BackendTarget(**fields)


def _assert_backend_call_leaves_no_state(name: str, instance: Any) -> None:
    """The literal SPEC §12 item 47 check, applied twice: once at construction (the registered
    singleton's `__dict__` must already be empty) and once after two `declared_capabilities()`
    calls for two DIFFERENT targets (it must STAY empty) — the same cross-repo-leak concern §7.2
    states for adapters, applied here to two calls sharing one singleton instead of two different
    repos. `assert_stateless` (`fleet.workers.base`) is the same helper `workers`/`manifests`/
    `ecosystems` are checked with; this is deliberately not a "no new attribute relative to a
    non-empty before" diff — the registered instance starts empty, so the stronger, literal
    `vars(inst) == {}` applies without qualification at both points.
    """
    assert_stateless(instance)
    instance.declared_capabilities(_target_for(name, suffix="one"))
    instance.declared_capabilities(_target_for(name, suffix="two"))
    try:
        assert_stateless(instance)
    except TypeError as exc:
        raise AssertionError(
            f"{type(instance).__qualname__} ({name!r}) picked up instance state across calls: {exc}"
        ) from exc


def test_every_registered_backend_is_stateless() -> None:
    """`vars(inst) == {}` for every registered backend instance (SPEC §12 item 47) — checked
    against the REAL registered singleton via `discover()`, not a fresh `cls()`: unlike workers, a
    backend constructor takes optional test-only collaborator overrides, and only the instance
    `register_backend` actually built with zero arguments is bound by this criterion.
    """
    backends = client_module.discover()
    assert backends, "the backends registry discovered nothing on this host"
    for name, instance in backends.items():
        _assert_backend_call_leaves_no_state(name, instance)


def test_backends_registry_is_a_stable_singleton() -> None:
    """Registration is a ONE-TIME construction (`register_backend`'s `cls()`, run once at import
    time) — re-discovery must return the SAME object, not a fresh instance each call."""
    first = client_module.discover()
    second = client_module.discover()
    assert first, "the backends registry discovered nothing on this host"
    for name, instance in first.items():
        assert second[name] is instance, f"{name}: discover() re-constructed a registered backend"


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
    with pytest.raises(AssertionError, match="picked up instance state across calls"):
        _assert_backend_call_leaves_no_state(_LeakyBackend.name, leaky)


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


# =======================================================================================
# 4. `register_worker`'s own duplicate-name guard — untested until now, and the registry's
#    module docstring calls it out explicitly: "A duplicate `name` is a startup error, never a
#    silent overwrite, because two workers answering to one name means the run's provenance is a
#    lie." Every sweep above only ever sees the REAL, already-unique registry `discover()`
#    populates, so none of them can express a collision. Two throwaway probe classes under a name
#    no real worker uses, with the module's global dicts snapshotted and restored via
#    reset_registry()-equivalent surgery so this cannot leak into any other test's registry state.
# =======================================================================================


def _make_probe_worker(qualname: str) -> type[BaseWorker]:  # type: ignore[type-arg]
    """A minimal, fully-concrete `BaseWorker` subclass under `registry`'s reserved probe name —
    concrete enough to satisfy `register_worker`, which only reads `cls.name`."""

    class _Probe(BaseWorker[WorkerInput, WorkerOutput]):
        name: ClassVar[str] = "probe-duplicate-name-guard"
        phase: ClassVar[Phase] = Phase.TRANSFORM
        input_model: ClassVar[type[WorkerInput]] = WorkerInput
        output_model: ClassVar[type[WorkerOutput]] = WorkerOutput

        async def run(self, ctx: WorkerContext, payload: WorkerInput) -> WorkerResult[WorkerOutput]:
            raise NotImplementedError  # pragma: no cover - never invoked

        async def preconditions_hold(self, ctx: WorkerContext, payload: WorkerInput) -> bool:
            return True

    _Probe.__qualname__ = qualname
    _Probe.__name__ = qualname
    return _Probe


def test_register_worker_rejects_a_duplicate_name_from_a_different_class() -> None:
    """A mutation that dropped or weakened the `existing is not None and existing is not cls`
    check in `register_worker` would pass every other test in this file unnoticed — they all walk
    the real registry, which by construction never contains a collision. Restores the registry's
    exact prior state in `finally` so this cannot perturb
    `test_every_registered_worker_is_stateless` or any other test that runs `discover()`."""
    saved_workers = dict(registry._WORKERS)
    saved_discovered = registry._DISCOVERED
    try:
        probe_a = _make_probe_worker("_ProbeA")
        probe_b = _make_probe_worker("_ProbeB")
        registry.register_worker(probe_a)
        with pytest.raises(RuntimeError, match="duplicate worker name"):
            registry.register_worker(probe_b)
        # re-registering the SAME class under the same name is idempotent, not a collision —
        # module re-import (e.g. under test collection) must not spuriously raise.
        registry.register_worker(probe_a)
        assert registry.registry()["probe-duplicate-name-guard"] is probe_a
    finally:
        registry._WORKERS.clear()
        registry._WORKERS.update(saved_workers)
        registry._DISCOVERED = saved_discovered
