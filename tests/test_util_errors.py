"""Mutation-proof tests for `fleet.util.errors.exception_type_name` (§15.1 item 3, round VIII).

`exception_type_name` is the single place every `exception_type=` field in the harness is
computed (module docstring). It is a one-line function, but the exact spelling it produces is
load-bearing: `docs/SPEC.md`/`WorkerError.exception_type` documents it as "qualified name only" —
`<module>.<qualname>` — and `workers/base.py::error_from_exception` (a real call site) feeds it
straight into a `WorkerError` that downstream classification and reporting code reads back.

These tests exercise:
  * the plain case (builtin exception),
  * the two properties that distinguish `__qualname__` from `__name__` and distinguish including
    the module from omitting it — a *nested* class name and a same-`__name__`-different-module
    pair,
  * the exact dotted-path format (module first, then qualname, joined by exactly one `.`),
  * and the real call site (`error_from_exception`) to prove the module is exercised through
    actual harness usage, not just in isolation.

Each mutation below is documented with the exact line changed and reproduced pass/fail evidence
in `.superpowers/sdd/round-VIII-qa-qc/worker-errors-mutation-proof-report.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fleet.util.errors import exception_type_name  # noqa: E402
from fleet.workers.base import error_from_exception  # noqa: E402


def test_builtin_exception_is_module_dot_qualname() -> None:
    """A plain builtin exception: module is `builtins`, qualname is the class name."""
    exc = ValueError("boom")
    assert exception_type_name(exc) == "builtins.ValueError"


def test_format_is_exactly_one_dot_joining_module_and_qualname() -> None:
    """The join is `f"{module}.{qualname}"` — module first, exactly one separating dot.

    A mutation that reverses the operand order (qualname first) or changes the separator
    produces a value that no longer starts with the module name, which this pins down
    independently of the nested-class / duplicate-name tests below.
    """
    exc = RuntimeError("x")
    result = exception_type_name(exc)
    module = type(exc).__module__
    qualname = type(exc).__qualname__
    assert result == f"{module}.{qualname}"
    assert result.startswith(module + ".")
    assert result.endswith("." + qualname)


class _Outer:
    class _Nested(Exception):
        """A nested exception class: `__qualname__` differs from `__name__` for this class."""


def test_nested_class_uses_qualname_not_bare_name() -> None:
    """`__qualname__` for a nested class includes the enclosing scope; `__name__` does not.

    This is the discriminator for the "use __name__ instead of __qualname__" mutation: on the
    real code the nested exception's recorded type name embeds `_Outer.`, which a `__name__`-based
    implementation can never produce.
    """
    exc = _Outer._Nested("nested boom")
    result = exception_type_name(exc)
    assert result == f"{__name__}._Outer._Nested"
    # The property a __name__-based mutation breaks:
    assert result != f"{__name__}.{type(exc).__name__}"
    assert type(exc).__qualname__ != type(exc).__name__  # sanity: this class IS nested


def test_same_bare_name_different_module_are_distinguished_by_module_prefix() -> None:
    """Two classes named `TimeoutError` (builtin vs. locally defined) must not collide.

    A mutation that drops the module and returns only `__qualname__` (or `__name__`) makes both
    of these equal to the bare string `"TimeoutError"`, silently merging two unrelated exception
    types under one recorded `exception_type` value. The harness relies on the full dotted path
    (not the bare name) to disambiguate exception types across modules (module docstring: "the
    one dotted path spelling every `exception_type=` field ... records").
    """

    class TimeoutError(Exception):  # deliberately shadowing the builtin name
        pass

    import builtins as _builtins_mod

    builtin_exc = _builtins_mod.TimeoutError()
    local_exc = TimeoutError()

    builtin_name = exception_type_name(builtin_exc)
    local_name = exception_type_name(local_exc)

    assert type(builtin_exc).__name__ == type(local_exc).__name__ == "TimeoutError"
    assert builtin_name != local_name
    assert builtin_name == "builtins.TimeoutError"
    assert local_name.endswith(".TimeoutError")
    assert not local_name.startswith("builtins.")


def test_real_call_site_error_from_exception_embeds_dotted_path() -> None:
    """`workers/base.py::error_from_exception` is a real call site: prove the module is exercised
    through actual harness usage, not just in isolation (brief requirement).

    The mutation this pins: `error_from_exception` must record the SAME string
    `exception_type_name` computes standalone, not a hand-rolled or stale spelling.
    """

    def _raise() -> None:
        raise KeyError("missing")

    try:
        _raise()
    except KeyError as exc:
        worker_error = error_from_exception(exc)
        assert worker_error.exception_type == exception_type_name(exc)
        assert worker_error.exception_type == "builtins.KeyError"


def test_exception_instance_state_does_not_affect_the_result() -> None:
    """The function classifies by TYPE, not by instance state (args/message).

    Guards against a mutation that accidentally reads `exc.args` or `str(exc)` into the result
    instead of `type(exc)`.
    """
    other = ValueError("totally different message")
    assert exception_type_name(ValueError("a")) == exception_type_name(other)
    assert exception_type_name(ValueError()) == "builtins.ValueError"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
