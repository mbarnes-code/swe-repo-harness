"""role → tier → ordered `BackendTarget` routing (ADR-0023, SPEC §9, §7.7's `RoleRouter`).

Two levels, and the separation is the point (§9): `roles` maps a named job to a **capability
tier** and is essentially fixed — it encodes ADR-0008's cost-per-judgment argument, not a vendor
preference; `profiles` maps each tier to an ordered list of backend targets and is what an
operator edits. Re-tiering a role is a config edit; swapping the whole fleet from hosted to local
models is a `--profile` flag and nothing else.

`LlmRouter` refuses any role that is not declared in config, and refuses a declared role whose
tier has no target — **both at construction**, not at first use. That is the whole reason this
class validates eagerly: a `KeyError` in wave 7, after 200 repos have been cloned and transformed,
is the failure mode §13 row 36 exists to eliminate. Nothing here reads a file, so `fleet models
list` (§10) can resolve the entire routing surface offline.

No model id, no `base_url` and no vendor name is written in this module: every one of them arrives
as data on a `BackendTarget` (§12.40).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from typing import TYPE_CHECKING, Final, final

from fleet.llm.client import LlmError, RoleRouter, TierRoute, UnknownRole
from fleet.models.enums import ModelTier
from fleet.models.tasks import BackendTarget

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance only; settings never imports llm
    from fleet.settings import ModelsConfig

__all__ = [
    "SPEC_ROLE_TIERS",
    "LlmRouter",
    "Role",
    "TierNotConfigured",
    "UnknownProfile",
]


class Role(StrEnum):
    """The named jobs a model is allowed to do (§9 `roles`, verbatim and exhaustive).

    A `StrEnum` rather than bare strings because three other modules key off these names —
    `schemas.RESPONSE_SCHEMAS`, `calls.PROMPTS`, and the `llm_cache` key — and a typo in any one
    of them would otherwise be a silent cache miss forever rather than a startup error. The
    *value* is what `config/models.yaml` and `ModelClient.complete(role=...)` carry, so the enum
    costs nothing at the boundary.
    """

    CONFLICT_RESOLUTION = "conflict_resolution"
    API_INCOMPAT_REWRITE = "api_incompat_rewrite"
    BUILD_AUTHORING = "build_authoring"
    CYCLE_BREAK_PROPOSAL = "cycle_break_proposal"
    ESCALATION = "escalation"
    TRANSFORM_REPAIR = "transform_repair"
    BUILD_DIAGNOSIS = "build_diagnosis"
    MANIFEST_EXTRACT = "manifest_extract"
    PR_BODY = "pr_body"
    REPO_CLASSIFY = "repo_classify"
    DEP_DISAMBIGUATE = "dep_disambiguate"
    PR_TITLE = "pr_title"


SPEC_ROLE_TIERS: Final[Mapping[Role, ModelTier]] = {
    Role.CONFLICT_RESOLUTION: ModelTier.HEAVY,
    Role.API_INCOMPAT_REWRITE: ModelTier.HEAVY,
    Role.BUILD_AUTHORING: ModelTier.HEAVY,
    Role.CYCLE_BREAK_PROPOSAL: ModelTier.HEAVY,
    Role.ESCALATION: ModelTier.HEAVY,  # ADR-0014 attempt 3
    Role.TRANSFORM_REPAIR: ModelTier.WORKHORSE,  # ADR-0014 attempt 2
    Role.BUILD_DIAGNOSIS: ModelTier.WORKHORSE,
    Role.MANIFEST_EXTRACT: ModelTier.WORKHORSE,
    Role.PR_BODY: ModelTier.WORKHORSE,
    Role.REPO_CLASSIFY: ModelTier.CHEAP,
    Role.DEP_DISAMBIGUATE: ModelTier.CHEAP,
    Role.PR_TITLE: ModelTier.CHEAP,
}
"""§9's `roles:` block as shipped. **Documentation and a default for tests, never authority**:
the active `config/models.yaml` decides, which is what makes re-tiering a config edit. It is
exported so `fleet models list` can show an operator where their file diverges from the shipped
routing, and so a test can build a realistic profile without duplicating the table."""


class TierNotConfigured(LlmError):
    """A tier some role routes to has no target in the active profile — §9 loader rule 1 and
    §13 row 36. A startup error, never a runtime `KeyError`, and never a silent fallback to a
    neighbouring tier: a role the operator sent to HEAVY being quietly answered by CHEAP is
    exactly the substitution ADR-0023 makes the cache key defend against."""

    def __init__(self, profile: str, tier: ModelTier, *, role: str | None = None) -> None:
        via = f" (routed there by role {role!r})" if role else ""
        super().__init__(f"profile {profile!r} configures no target for tier {tier}{via}")
        self.profile = profile
        self.tier = tier
        self.role = role


class UnknownProfile(LlmError):
    """`--profile`/`llm.profile` names a profile `config/models.yaml` does not define. Startup,
    with the available names listed, because the operator's next action is to pick one."""

    def __init__(self, profile: str, available: Iterable[str]) -> None:
        super().__init__(
            f"no profile named {profile!r} in config/models.yaml; have {sorted(available)}",
        )
        self.profile = profile


@final
class LlmRouter:
    """`RoleRouter` over an already-parsed `config/models.yaml` (§7.7).

    Takes the parsed two-level mapping, never a path: the YAML is `settings.py`'s job, so this
    class stays a pure function of its inputs and a test does not need a file on disk.
    """

    def __init__(
        self,
        roles: Mapping[str, ModelTier],
        targets: Mapping[ModelTier, Sequence[BackendTarget]],
        *,
        profile: str = "default",
        required_roles: Iterable[str] | None = None,
    ) -> None:
        """Resolve and validate the whole surface now, so nothing can fail later.

        `required_roles` defaults to every `Role`: the harness binds a prompt and a response
        schema to each one (`calls.py`, `schemas.py`), so a `models.yaml` missing one has a job
        with no model behind it. That is `UnknownRole` at startup, naming the role — the same
        error `resolve()` raises, because it is the same defect seen from the other side.
        """
        self._profile = profile
        self._roles: dict[str, ModelTier] = {
            str(role): ModelTier(tier) for role, tier in sorted(roles.items())
        }
        self._targets: dict[ModelTier, tuple[BackendTarget, ...]] = {
            ModelTier(tier): tuple(entries) for tier, entries in targets.items()
        }

        required = tuple(Role) if required_roles is None else tuple(required_roles)
        for role in required:
            if str(role) not in self._roles:
                raise UnknownRole(str(role))
        for role, tier in self._roles.items():
            if not self._targets.get(tier):
                raise TierNotConfigured(profile, tier, role=role)
            # Build the route now, so `TierRoute`'s own validation (one `effort` per
            # `(backend, model_id)`, which the LLM cache's failover attribution depends on)
            # fails HERE and not at the first model call, halfway through a run.
            self.resolve(role)

    @classmethod
    def from_models_config(
        cls,
        models: ModelsConfig,
        *,
        profile: str | None = None,
        required_roles: Iterable[str] | None = None,
    ) -> LlmRouter:
        """Build from the typed `config/models.yaml` tree. `profile` defaults to the file's
        `default_profile`; the run's `--profile` override is resolved by `settings.py` and passed
        in here, so this class never learns what a CLI flag is."""
        active = profile or models.default_profile
        if active not in models.profiles:
            raise UnknownProfile(active, models.profiles)
        return cls(
            models.roles,
            models.profiles[active],
            profile=active,
            required_roles=required_roles,
        )

    @property
    def profile(self) -> str:
        return self._profile

    def roles(self) -> tuple[str, ...]:
        """Every declared role, sorted — a total ordering, so `fleet models list` output and any
        digest computed over it are deterministic (§11.6)."""
        return tuple(self._roles)

    def tier_for(self, role: str) -> ModelTier:
        tier = self._roles.get(role)
        if tier is None:
            raise UnknownRole(role)
        return tier

    def resolve(self, role: str, *, tier_override: ModelTier | None = None) -> TierRoute:
        """role → `TierRoute`. `tier_override` is the operator escape hatch (§7.7) and is audited
        as a finding by the caller; it moves the *tier*, never the role's existence — an
        undeclared role is `UnknownRole` even with an override, because the override says where to
        send a job, not that the job exists."""
        declared = self.tier_for(role)
        tier = declared if tier_override is None else tier_override
        targets = self._targets.get(tier, ())
        if not targets:
            raise TierNotConfigured(self._profile, tier, role=role)
        return TierRoute(tier=tier, targets=targets)

    def routes(self) -> tuple[tuple[str, TierRoute], ...]:
        """Every role's resolution, in role order — the offline dry-run `fleet models list` prints
        and §13 row 36's startup gate walks."""
        return tuple((role, self.resolve(role)) for role in self._roles)


def _protocol_conformance(router: LlmRouter) -> RoleRouter:
    """`mypy --strict` proof that `LlmRouter` satisfies §7.7's `RoleRouter`. Structural typing is
    checked at use sites only, and the only use site is a client this module does not import — so
    without this line a signature drift here would surface as a break somewhere else."""
    return router
