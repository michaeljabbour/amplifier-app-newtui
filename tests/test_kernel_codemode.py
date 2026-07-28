"""The Code Mode Python execution backend — real subprocess sandbox behavior.

Every test spawns a genuine restricted child interpreter (offline, no network)
and asserts the donor laws (`.ai/oc_donor.md`): tools bridge back to the host,
expected failures are DATA (a diagnostic) not exceptions, and the child has no
ambient authority (no import / open / dunder introspection).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from amplifier_app_newtui.kernel.codemode import (
    SandboxRunner,
    ToolInvokerError,
    audit_program,
)
from amplifier_app_newtui.model.codemode import DiagnosticKind, ExecutionLimits

# Generous wall-clock guard so a harness bug fails fast instead of hanging CI.
_SAFE = ExecutionLimits(timeout_ms=10_000)


class _Recorder:
    """A host tool invoker that records calls and returns scripted results."""

    def __init__(self, fn: Any) -> None:
        self.calls: list[tuple[str, Mapping[str, Any]]] = []
        self._fn = fn

    def __call__(self, name: str, tool_input: Mapping[str, Any]) -> Any:
        self.calls.append((name, tool_input))
        return self._fn(name, tool_input)


def _run(code: str, invoker: Any = None, *, limits: ExecutionLimits | None = _SAFE):
    invoker = invoker or (lambda name, arg: None)
    return SandboxRunner(invoker, limits=limits).run(code)


def test_pure_program_returns_value_without_tools() -> None:
    result = _run("return 21 * 2")
    assert result.ok is True
    assert result.value == 42
    assert result.tool_calls == ()


def test_bridged_tool_call_returns_host_result() -> None:
    invoker = _Recorder(lambda name, arg: {"content": f"read {arg['path']}"})
    result = _run('return tools.read.read_file({"path": "a.py"})["content"]', invoker)
    assert result.ok is True
    assert result.value == "read a.py"
    assert invoker.calls == [("read.read_file", {"path": "a.py"})]
    assert [c.name for c in result.tool_calls] == ["read.read_file"]
    assert result.tool_calls[0].status == "completed"


def test_many_calls_run_in_one_pass() -> None:
    invoker = _Recorder(lambda name, arg: arg["n"] + 1)
    code = (
        "total = 0\nfor i in [1, 2, 3]:\n    total = total + tools.math.inc({'n': i})\nreturn total"
    )
    result = _run(code, invoker)
    assert result.ok is True
    assert result.value == 9  # (1+1)+(2+1)+(3+1)
    assert len(invoker.calls) == 3
    assert len(result.tool_calls) == 3


def test_log_is_captured_and_rendered() -> None:
    result = _run("log('start')\nlog('done', 42)\nreturn 1")
    assert result.ok is True
    assert result.logs == ("start", "done 42")
    assert result.render_output() == "1\n\nLogs:\nstart\ndone 42"


def test_max_tool_calls_limit_is_a_diagnostic() -> None:
    invoker = _Recorder(lambda name, arg: 1)
    code = "for i in [1, 2, 3]:\n    tools.x.y({})\nreturn 'unreached'"
    result = _run(code, invoker, limits=ExecutionLimits(timeout_ms=10_000, max_tool_calls=2))
    assert result.ok is False
    assert result.diagnostic is not None
    assert result.diagnostic.kind is DiagnosticKind.TOOL_CALL_LIMIT_EXCEEDED
    assert len(invoker.calls) == 2  # the 3rd call is refused, not invoked


def test_uncaught_tool_failure_becomes_tool_failure_diagnostic() -> None:
    def boom(name: str, arg: Mapping[str, Any]) -> Any:
        raise ToolInvokerError("order is unavailable")

    result = _run("return tools.orders.lookup({'id': 'x'})", boom)
    assert result.ok is False
    assert result.diagnostic is not None
    assert result.diagnostic.kind is DiagnosticKind.TOOL_FAILURE
    assert "order is unavailable" in result.diagnostic.message
    assert result.tool_calls[0].status == "error"


def test_tool_failure_is_catchable_in_program() -> None:
    def boom(name: str, arg: Mapping[str, Any]) -> Any:
        raise ToolInvokerError("boom")

    code = (
        "try:\n    r = tools.x.y({})\nexcept Exception as e:\n    r = 'caught:' + str(e)\nreturn r"
    )
    result = _run(code, boom)
    assert result.ok is True
    assert result.value == "caught:boom"


def test_unknown_host_failure_is_sanitized() -> None:
    def explode(name: str, arg: Mapping[str, Any]) -> Any:
        raise RuntimeError("secret internal path /etc/keys")

    result = _run("return tools.x.y({})", explode)
    assert result.ok is False
    assert result.diagnostic is not None
    # The private cause never crosses the boundary (donor law 4).
    assert "secret internal path" not in result.diagnostic.message


def test_import_is_rejected() -> None:
    result = _run("import os\nreturn 1")
    assert result.ok is False
    assert result.diagnostic is not None
    assert result.diagnostic.kind is DiagnosticKind.UNSUPPORTED_SYNTAX


def test_dunder_introspection_is_rejected() -> None:
    result = _run("return ().__class__.__bases__")
    assert result.ok is False
    assert result.diagnostic is not None
    assert result.diagnostic.kind is DiagnosticKind.UNSUPPORTED_SYNTAX


def test_open_is_unavailable_in_the_sandbox() -> None:
    result = _run("return open('/etc/passwd')")
    assert result.ok is False
    assert result.diagnostic is not None
    assert result.diagnostic.kind is DiagnosticKind.EXECUTION_FAILURE
    assert "open" in result.diagnostic.message


def test_syntax_error_is_a_parse_error() -> None:
    result = _run("return (1 +")
    assert result.ok is False
    assert result.diagnostic is not None
    assert result.diagnostic.kind is DiagnosticKind.PARSE_ERROR


def test_timeout_is_a_diagnostic() -> None:
    result = _run("while True:\n    pass\n", limits=ExecutionLimits(timeout_ms=400))
    assert result.ok is False
    assert result.diagnostic is not None
    assert result.diagnostic.kind is DiagnosticKind.TIMEOUT_EXCEEDED


def test_output_is_truncated_to_budget() -> None:
    code = "return 'x' * 5000"
    result = _run(code, limits=ExecutionLimits(timeout_ms=10_000, max_output_bytes=64))
    assert result.ok is True
    assert result.truncated is True
    assert "output truncated" in result.render_output()


def test_audit_program_allows_top_level_return() -> None:
    # Top-level return is valid in code mode (donor parity) — audit wraps it.
    assert audit_program("return tools.x.y({})") is None
    assert audit_program("import sys") is not None
