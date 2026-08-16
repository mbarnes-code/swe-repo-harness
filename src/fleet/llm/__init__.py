"""The model boundary (SPEC §7.7, ADR-0002/0008/0023).

Four modules, one job each: `client.py` is the protocol and the reference ladder client,
`roles.py` routes a role to a tier and its ordered targets, `schemas.py` declares what each role's
reply must be, `calls.py` binds prompt to schema per role, and `cache.py` makes a re-run
reproducible without pinning a temperature.

Import from here, not from a submodule, and never from a vendor SDK: no model id, no `base_url`
and no provider name crosses this package's boundary (§12.40).
"""

from __future__ import annotations

from fleet.llm.cache import (
    EMPTY_SHA256,
    CacheKeyParts,
    CacheMiss,
    CacheMode,
    CacheRow,
    CachingModelClient,
    LlmCacheStore,
    MemoryLlmCacheStore,
    SqliteLlmCacheStore,
)
from fleet.llm.calls import (
    PROMPTS,
    Evidence,
    PromptTemplate,
    prompt_sha256,
    prompt_template_version,
    render_prompt,
)
from fleet.llm.client import (
    CallBudget,
    LlmError,
    Message,
    ModelBackend,
    ModelClient,
    ModelResponse,
    RoleRouter,
    TierRoute,
    UnknownRole,
    discover,
    register_backend,
)
from fleet.llm.roles import SPEC_ROLE_TIERS, LlmRouter, Role, TierNotConfigured, UnknownProfile
from fleet.llm.schemas import RESPONSE_SCHEMAS, response_model_for, response_schema_sha256

__all__ = [
    "EMPTY_SHA256",
    "PROMPTS",
    "RESPONSE_SCHEMAS",
    "SPEC_ROLE_TIERS",
    "CacheKeyParts",
    "CacheMiss",
    "CacheMode",
    "CacheRow",
    "CachingModelClient",
    "CallBudget",
    "Evidence",
    "LlmCacheStore",
    "LlmError",
    "LlmRouter",
    "MemoryLlmCacheStore",
    "Message",
    "ModelBackend",
    "ModelClient",
    "ModelResponse",
    "PromptTemplate",
    "Role",
    "RoleRouter",
    "SqliteLlmCacheStore",
    "TierNotConfigured",
    "TierRoute",
    "UnknownProfile",
    "UnknownRole",
    "discover",
    "prompt_sha256",
    "prompt_template_version",
    "register_backend",
    "render_prompt",
    "response_model_for",
    "response_schema_sha256",
]
