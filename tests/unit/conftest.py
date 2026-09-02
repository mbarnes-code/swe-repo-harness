"""§12.3 (`docs/SPEC.md`): `pytest tests/unit -q` must complete in <30s with no network, no
Docker, and **no provider credential of any kind present** in the environment, "asserted by the
test runner clearing every `api_key_env` named by every profile in `config/models.yaml`" (SPEC's
own literal wording — not a paraphrase).

Every name is collected from the RAW yaml, not through `fleet.settings.FleetConfig.load()`: that
loader validates `fleet.yaml` and `repos.yaml` too, which is more machinery than "read the
declared credential variable names" needs, and would make this fixture's correctness depend on
files this criterion says nothing about. A plain recursive walk collecting every `api_key_env`
leaf is the whole job, and is easy to audit against SPEC's sentence directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

_MODELS_YAML = Path(__file__).resolve().parents[2] / "config" / "models.yaml"


def _collect_api_key_env_names(node: Any) -> set[str]:
    """Recursively walk the raw parsed yaml, returning every value found under an
    `api_key_env` key anywhere in the structure (any profile, any tier, any target) — this is
    deliberately not scoped to the active/default profile, per SPEC's "every profile"."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "api_key_env" and isinstance(value, str):
                found.add(value)
            else:
                found |= _collect_api_key_env_names(value)
    elif isinstance(node, list):
        for item in node:
            found |= _collect_api_key_env_names(item)
    return found


@pytest.fixture(scope="session")
def provider_credential_env_names() -> frozenset[str]:
    """Every `api_key_env` variable name named by any profile in `config/models.yaml`, for tests
    that want to assert directly on the set (e.g. that it's non-empty, so this fixture isn't
    silently a no-op on a stripped-down config)."""
    data = yaml.safe_load(_MODELS_YAML.read_text(encoding="utf-8"))
    return frozenset(_collect_api_key_env_names(data))


@pytest.fixture(autouse=True)
def _no_provider_credentials(
    monkeypatch: pytest.MonkeyPatch, provider_credential_env_names: frozenset[str]
) -> None:
    """Clears every named credential variable from the environment for the duration of each
    `tests/unit` test — this IS the "asserted by the test runner clearing" mechanism SPEC's
    §12.3 sentence names. `monkeypatch.delenv(..., raising=False)` restores the prior value (set
    or unset) after the test, so this cannot leak a real key out of one test into the next, and
    cannot fail a developer's shell that legitimately has one exported for other work."""
    for name in provider_credential_env_names:
        monkeypatch.delenv(name, raising=False)
