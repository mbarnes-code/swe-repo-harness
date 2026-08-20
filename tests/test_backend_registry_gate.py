"""ADR-0078 — §9 rule 2's accepted `backend:` names are the LIVE §7.7 registry, nothing wider.

`fleet.cli._load_settings` calls `fleet.llm.client.discover()` and hands its keys to
`FleetSettings.load(known_backends=...)`. `settings.SHIPPED_BACKENDS` names four transports; only
the ones whose vendor SDK actually imported on this host register, so the accepted set is a
*subset* of the shipped names and is a proper subset on any host missing an extra. That narrowing
is the behaviour ADR-0078 records, and it had no test: the nearest one
(`tests/test_llm_backend_anthropic.py::test_the_startup_gate_checks_the_live_registry_not_the_
shipped_name_tuple`) asserts a disjunction that a *superset* satisfies, so widening the set back
out passes it silently.

Both tests here drive the real `_load_settings`. Neither opens a socket and neither imports a
vendor SDK directly — `discover()` imports whichever adapters this host can, which is the point.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from fleet import cli as cli_module
from fleet.llm.client import discover
from fleet.settings import SHIPPED_BACKENDS, UnresolvedReferenceError

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The shipped `config/models.yaml` CHEAP target, and the same target rerouted to a backend that
#: `SHIPPED_BACKENDS` names but the registry in test 2 does not hold. `region` is supplied so the
#: refusal can only come from the rule 2 registry gate and never from `_REQUIRED_TARGET_FIELDS`.
_SHIPPED_CHEAP = "- { backend: anthropic, model_id: claude-haiku-4-5,"
_BEDROCK_CHEAP = "- { backend: bedrock, model_id: anthropic.claude-haiku, region: us-east-1,"


class _Stop(Exception):
    """Ends `_load_settings` as soon as the spy has recorded what it was called with."""


def _config_dir_routing_cheap_to_bedrock(tmp_path: Path) -> Path:
    """A copy of the shipped `config/` whose default profile routes CHEAP through `bedrock`.

    The shipped directory rather than a hand-written one: everything except the backend name is
    then known-valid, so a failure to load can only be the thing under test.
    """
    config_dir = tmp_path / "config"
    shutil.copytree(REPO_ROOT / "config", config_dir)
    models = config_dir / "models.yaml"
    text = models.read_text(encoding="utf-8")
    assert text.count(_SHIPPED_CHEAP) == 1, (
        "config/models.yaml no longer contains the CHEAP target this fixture rewrites; the test "
        "would otherwise load an unmodified config and pass without exercising the gate"
    )
    models.write_text(text.replace(_SHIPPED_CHEAP, _BEDROCK_CHEAP), encoding="utf-8")
    return config_dir


def test_startup_hands_the_gate_exactly_the_live_registry_and_never_a_superset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WHY: the narrowing is an EQUALITY, and only an equality assertion binds it.

    §9 rule 2 must mean "this backend is registered", not "this name is spelled like one we ship".
    The failure mode a membership or inequality check cannot see is a *superset*: hand the gate
    `tuple(discover()) + ("bedrock",)` and a profile naming `bedrock` boots on a host where the
    adapter's SDK never imported, then raises `UnknownBackend` from `LadderModelClient` in wave 7
    with repos already cloned — which is exactly the vacuous §13 row 36 gate BK1 removed, restored
    for one name at a time. `set(passed) == set(discover())` is the only shape that fails on it.

    The spy stops `_load_settings` before any file is read, so this asserts the argument the
    startup path constructs and nothing about the config on disk.
    """
    seen: dict[str, Any] = {}

    def spy(config_dir: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise _Stop

    monkeypatch.setattr(cli_module.FleetSettings, "load", spy)
    with pytest.raises(_Stop):
        cli_module._load_settings(cli_module.GlobalOptions())

    passed = seen.get("known_backends")
    assert passed is not None, (
        "known_backends was not supplied, so the gate falls back to the SHIPPED_BACKENDS constant "
        "and validates spelling instead of registration"
    )
    assert set(passed) == set(discover()), (
        f"the startup gate's name set is not the live registry: passed {sorted(set(passed))}, "
        f"registry holds {sorted(discover())}"
    )
    # ...and the live registry can only ever be a subset of what we ship, so a name reaching the
    # gate from anywhere else is a widening by another route.
    assert set(passed) <= set(SHIPPED_BACKENDS)


def test_a_shipped_name_the_live_registry_lacks_is_refused_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WHY: the consequence of the narrowing, end to end, on a host that cannot serve the target.

    `bedrock` is in `SHIPPED_BACKENDS` and ships behind the `fleet[bedrock]` extra. With the
    registry narrowed to the adapters that imported, a profile naming it must be refused HERE —
    at startup, naming the profile, tier and target index — not accepted because the name is
    spelled correctly. This drives the whole path (`_load_settings` -> `discover()` ->
    `FleetSettings.load(known_backends=...)` -> `_check_routing`), so it fails if the CLI widens
    the set OR if the gate itself starts waving through a `SHIPPED_BACKENDS` name.

    `discover` is replaced rather than relying on this host's missing SDKs: the assertion must
    hold identically on a host that installs all four extras, where the live registry and the
    shipped tuple coincide and the narrowing would otherwise be untestable.
    """
    monkeypatch.setattr(cli_module, "discover", lambda: {"anthropic": object()})
    for name in [key for key in os.environ if key.startswith("FLEET_")]:
        monkeypatch.delenv(name, raising=False)
    config_dir = _config_dir_routing_cheap_to_bedrock(tmp_path)

    with pytest.raises(UnresolvedReferenceError) as excinfo:
        cli_module._load_settings(
            cli_module.GlobalOptions(config_path=config_dir / "fleet.yaml")
        )

    message = str(excinfo.value)
    assert "'bedrock'" in message                      # THIS backend, from the rule 2 gate
    assert "is not in the §7.7 registry" in message    # ...and not some earlier check
    assert "fleet[bedrock]" in message                 # a missing extra, not a typo
    assert "['anthropic']" in message                  # the LIVE set, reported as what it is
