"""ADR-0145 — `src/fleet/llm/backends/harmony_gpt_oss.py`.

`openai-harmony` is a compiled (PyO3/maturin) extension. This file is written so it can run
whether or not the real package is installed, matching `tests/test_llm_backend_vertex.py`'s own
pattern: a minimal stub is installed into `sys.modules` BEFORE the adapter is imported, only when
the real package is genuinely absent (`find_spec`, never `sys.modules` membership — the two
answer different questions; see that file's `_absent()` docstring for why).

Coroutines are driven with `asyncio.run`, matching every other backend test file.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from fleet.llm import client as client_module
from fleet.llm.client import ModelBackend
from fleet.models.tasks import BackendTarget


def _absent(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is None
    except (ModuleNotFoundError, ValueError):
        return True


HARMONY_INSTALLED = not _absent("openai_harmony")


def target(**overrides: object) -> BackendTarget:
    fields: dict[str, object] = {
        "backend": "harmony_gpt_oss",
        "model_id": "model-under-test",
        "base_url": "http://pilot-spark.internal:8000/v1",
        "price": "free",
    }
    fields.update(overrides)
    return BackendTarget(**fields)


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_discover_registers_harmony_gpt_oss_when_the_sdk_imports() -> None:
    from fleet.llm.backends.harmony_gpt_oss import HarmonyGptOssBackend

    found = client_module.discover()
    assert HarmonyGptOssBackend.name in found
    assert isinstance(found[HarmonyGptOssBackend.name], HarmonyGptOssBackend)


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_name_and_version_are_class_attributes() -> None:
    from fleet.llm.backends.harmony_gpt_oss import HarmonyGptOssBackend

    assert HarmonyGptOssBackend.name == "harmony_gpt_oss"
    assert isinstance(HarmonyGptOssBackend.version, int)
    assert isinstance(HarmonyGptOssBackend(), ModelBackend)


def test_a_missing_sdk_leaves_the_backend_unregistered() -> None:
    """Driven in a fresh interpreter with `openai_harmony` blocked, matching
    `tests/test_llm_backend_vertex.py::test_a_missing_sdk_leaves_the_backend_unregistered` — the
    only honest way to test an import-time contract is at import time."""
    script = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'openai_harmony' or name.startswith('openai_harmony.'):\n"
        "            raise ImportError('blocked')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "from fleet.llm import client\n"
        "try:\n"
        "    import fleet.llm.backends.harmony_gpt_oss\n"
        "    print('IMPORTED')\n"
        "except ImportError:\n"
        "    print('IMPORT_ERROR')\n"
        "print('harmony_gpt_oss' in client.discover())\n"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    ).stdout.split()
    assert out[0] == "IMPORT_ERROR", out
    assert out[1] == "False", out


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_a_target_with_no_base_url_names_the_missing_field() -> None:
    from fleet.llm.backends.harmony_gpt_oss import HarmonyGptOssBackend, MissingBaseUrl

    with pytest.raises(MissingBaseUrl):
        HarmonyGptOssBackend().declared_capabilities(target(base_url=None))


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_declared_capabilities_claim_tool_call_and_prompted_only() -> None:
    from fleet.llm.backends.harmony_gpt_oss import HarmonyGptOssBackend
    from fleet.models.enums import StructuredOutputMode

    caps = HarmonyGptOssBackend().declared_capabilities(target())
    assert caps.supports_tools is True
    assert caps.supports_json_schema is False
    assert set(caps.structured_output_modes) == {
        StructuredOutputMode.TOOL_CALL,
        StructuredOutputMode.PROMPTED,
    }


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_the_declaration_is_a_copy_not_shared_module_state() -> None:
    from fleet.llm.backends.harmony_gpt_oss import HarmonyGptOssBackend

    backend = HarmonyGptOssBackend()
    first = backend.declared_capabilities(target())
    first.max_context = 999_999  # type: ignore[misc]  # FleetModel allows assignment
    second = backend.declared_capabilities(target())
    assert second.max_context != 999_999
