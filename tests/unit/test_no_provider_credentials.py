"""Proves the `tests/unit/conftest.py` credential-clearing fixture actually does something,
rather than silently passing over an empty set (§12.3's "no provider credential of any kind
present ... asserted by the test runner clearing every `api_key_env`").
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest


def test_config_models_yaml_names_at_least_one_credential_variable(
    provider_credential_env_names: frozenset[str],
) -> None:
    """A non-empty set is what makes the autouse fixture non-vacuous. Today's
    `config/models.yaml` names `ANTHROPIC_API_KEY` and `LOCAL_LLM_API_KEY` at minimum."""
    assert provider_credential_env_names
    assert "ANTHROPIC_API_KEY" in provider_credential_env_names


def test_every_named_credential_variable_is_absent_from_the_environment(
    provider_credential_env_names: frozenset[str],
) -> None:
    for name in provider_credential_env_names:
        assert name not in os.environ, f"{name} is set — §12.3 forbids any provider credential"


@pytest.fixture(scope="module", autouse=True)
def _plant_a_credential_before_the_clearing_fixture_runs(
    provider_credential_env_names: frozenset[str],
) -> Iterator[str]:
    """Module-scoped, so pytest sets this up BEFORE the function-scoped, autouse
    `_no_provider_credentials` fixture in `tests/unit/conftest.py` runs (higher scope always
    sets up first, regardless of autouse ordering) — planting a real value in the environment
    that the clearing fixture must then remove before the test body below runs. Without this,
    the two tests above would also pass on a do-nothing fixture, since the ambient shell never
    had these vars set either — this is the discriminating case.
    """
    name = next(iter(provider_credential_env_names))
    os.environ[name] = "sk-should-never-survive-to-a-test-body"
    yield name
    os.environ.pop(name, None)


def test_the_clearing_fixture_removes_a_credential_planted_ahead_of_it(
    _plant_a_credential_before_the_clearing_fixture_runs: str,
) -> None:
    """Proves the mechanism, not just the ambient state: the module-scoped fixture above set a
    real value; if `_no_provider_credentials` (function-scoped, so it runs after) were a no-op,
    this assertion would fail."""
    name = _plant_a_credential_before_the_clearing_fixture_runs
    assert name not in os.environ, (
        f"{name} survived into the test body — the clearing fixture did not run or is a no-op"
    )
