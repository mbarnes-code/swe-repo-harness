"""LLM-boundary fixtures (SPEC §8). `echo_backend.py` is imported (not just read) by
`tests/test_llm_backend_fixture_e2e.py`, which is what triggers its `@register_backend`
decorator — see that module's docstring for why importing IS the registration mechanism.
"""
