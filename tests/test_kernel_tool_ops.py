"""Kernel tool-invocation seam: ``describe_tools`` / ``invoke_tool`` + trust gate.

The scriptable ``amplifier-newtui tool invoke`` runs on the SAME coordinator
surface the in-session ops already drive (``coordinator.get("tools")`` mapping
names to objects with an async ``execute`` returning a ``ToolResult``). These
tests exercise that seam with duck-typed fakes -- no real session, no network --
plus the one-shot governance gate (``kernel/tool_cli``) that refuses anything a
one-shot CLI cannot get an interactive approval for.
"""

from __future__ import annotations

import asyncio

from amplifier_app_newtui.kernel import session_ops, tool_cli
from amplifier_app_newtui.kernel.runtime import RealRuntime


class FakeResult:
    """A ``ToolResult`` stand-in (``.success`` / ``.output`` / ``.error``)."""

    def __init__(self, success: bool, output: object = None, error: object = None) -> None:
        self.success = success
        self.output = output
        self.error = error


class FakeTool:
    def __init__(
        self,
        *,
        description: str = "",
        result: object = None,
        raises: Exception | None = None,
    ) -> None:
        self.description = description
        self._result = result
        self._raises = raises
        self.calls: list[dict] = []

    async def execute(self, args: dict) -> object:
        self.calls.append(args)
        if self._raises is not None:
            raise self._raises
        return self._result


class NoExecTool:
    description = "listable but not callable"


class FakeCoordinator:
    def __init__(self, tools: object) -> None:
        self._tools = tools

    def get(self, name: str) -> object:
        return self._tools if name == "tools" else None


class DenyWritePolicy:
    """Duck-typed :class:`DirectoryPolicy` that blocks every write target."""

    def check_write(self, target: str) -> tuple[bool, str]:
        return (False, "path is outside the project write boundary")

    def within_allowed(self, target: str) -> bool:
        return False

    def check_read(self, target: str) -> tuple[bool, str]:
        return (True, "reads roam")

    def shell_outside_target(self, action: str):
        return None


# -- describe_tools ---------------------------------------------------------


def test_describe_tools_sorts_and_carries_summary_and_invokable() -> None:
    coord = FakeCoordinator(
        {
            "read_file": FakeTool(description="Reads a file.\nSecond line ignored."),
            "bash": FakeTool(description=""),
            "broken": NoExecTool(),
        }
    )
    rows = asyncio.run(session_ops.describe_tools(coord))
    assert [r.name for r in rows] == ["bash", "broken", "read_file"]
    read = next(r for r in rows if r.name == "read_file")
    assert read.description == "Reads a file."
    assert read.invokable is True
    assert next(r for r in rows if r.name == "broken").invokable is False


def test_describe_tools_uses_docstring_when_no_description() -> None:
    class DocTool:
        """Docstring summary here.\n\n    body ignored.\n"""

    rows = asyncio.run(session_ops.describe_tools(FakeCoordinator({"d": DocTool()})))
    assert rows[0].description == "Docstring summary here."


def test_describe_tools_non_dict_mount_is_empty() -> None:
    assert asyncio.run(session_ops.describe_tools(FakeCoordinator("nope"))) == ()


# -- invoke_tool (session_ops) ---------------------------------------------


def test_invoke_tool_success_returns_output() -> None:
    tool = FakeTool(result=FakeResult(True, output={"content": "hello"}))
    coord = FakeCoordinator({"read_file": tool})
    outcome = asyncio.run(session_ops.invoke_tool(coord, "read_file", {"file_path": "x"}))
    assert outcome.found and outcome.ok
    assert outcome.output == {"content": "hello"}
    assert tool.calls == [{"file_path": "x"}]


def test_invoke_tool_failure_extracts_error_message() -> None:
    coord = FakeCoordinator({"t": FakeTool(result=FakeResult(False, error={"message": "boom"}))})
    outcome = asyncio.run(session_ops.invoke_tool(coord, "t", {}))
    assert outcome.found and not outcome.ok
    assert outcome.error == "boom"


def test_invoke_tool_unknown_is_not_found() -> None:
    outcome = asyncio.run(session_ops.invoke_tool(FakeCoordinator({}), "ghost", {}))
    assert outcome.found is False and outcome.ok is False
    assert "ghost" in outcome.error


def test_invoke_tool_without_execute_reports_reason() -> None:
    outcome = asyncio.run(session_ops.invoke_tool(FakeCoordinator({"x": NoExecTool()}), "x", {}))
    assert outcome.found is True and outcome.ok is False
    assert "execute" in outcome.error


def test_invoke_tool_exception_is_captured() -> None:
    coord = FakeCoordinator({"t": FakeTool(raises=RuntimeError("kaboom"))})
    outcome = asyncio.run(session_ops.invoke_tool(coord, "t", {}))
    assert outcome.found is True and outcome.ok is False
    assert outcome.error == "kaboom"


def test_invoke_tool_bare_value_is_surfaced_as_output() -> None:
    coord = FakeCoordinator({"t": FakeTool(result="plain string")})
    outcome = asyncio.run(session_ops.invoke_tool(coord, "t", {}))
    assert outcome.ok and outcome.output == "plain string"


# -- gate_invocation (kernel/tool_cli) -------------------------------------


def test_gate_allows_reads_by_default() -> None:
    gate = tool_cli.gate_invocation(
        "read_file", {"file_path": "README.md"}, allow_writes=False, directory_policy=None
    )
    assert gate.allowed and gate.capability == "read"


def test_gate_refuses_writes_without_yes() -> None:
    gate = tool_cli.gate_invocation(
        "write_file", {"file_path": "out.txt"}, allow_writes=False, directory_policy=None
    )
    assert not gate.allowed and gate.capability == "write"
    assert "approval" in gate.reason


def test_gate_allows_in_project_write_with_yes() -> None:
    gate = tool_cli.gate_invocation(
        "write_file", {"file_path": "out.txt"}, allow_writes=True, directory_policy=None
    )
    assert gate.allowed and gate.capability == "write"


def test_gate_refuses_exec_even_with_yes() -> None:
    gate = tool_cli.gate_invocation(
        "bash", {"command": "ls"}, allow_writes=True, directory_policy=None
    )
    assert not gate.allowed and gate.capability == "exec"


def test_gate_blocks_out_of_boundary_write() -> None:
    gate = tool_cli.gate_invocation(
        "write_file",
        {"file_path": "/etc/passwd"},
        allow_writes=True,
        directory_policy=DenyWritePolicy(),
    )
    assert not gate.allowed
    assert "boundary" in gate.reason


# -- RealRuntime wiring -----------------------------------------------------


def _runtime_with(tools: dict) -> RealRuntime:
    runtime = RealRuntime()
    runtime._initialized = type("Init", (), {"coordinator": FakeCoordinator(tools)})()
    runtime.directory_policy = None
    return runtime


def test_runtime_describe_tools_delegates() -> None:
    runtime = _runtime_with({"read_file": FakeTool(description="Reads.")})
    rows = asyncio.run(runtime.describe_tools())
    assert [r.name for r in rows] == ["read_file"]


def test_runtime_invoke_tool_reads_through_gate() -> None:
    tool = FakeTool(result=FakeResult(True, output="ok"))
    runtime = _runtime_with({"read_file": tool})
    outcome = asyncio.run(runtime.invoke_tool("read_file", {"file_path": "x"}))
    assert outcome.ok and outcome.output == "ok"


def test_runtime_invoke_tool_unknown_is_not_found() -> None:
    runtime = _runtime_with({"read_file": FakeTool(result=FakeResult(True))})
    outcome = asyncio.run(runtime.invoke_tool("ghost", {}))
    assert outcome.found is False
    assert "ghost" in outcome.error


def test_runtime_invoke_tool_blocks_write_without_yes() -> None:
    tool = FakeTool(result=FakeResult(True, output="wrote"))
    runtime = _runtime_with({"write_file": tool})
    outcome = asyncio.run(runtime.invoke_tool("write_file", {"file_path": "x"}))
    assert outcome.found is True and outcome.ok is False
    assert outcome.blocked is True and outcome.capability == "write"
    assert tool.calls == []  # never executed


def test_runtime_invoke_tool_allows_write_with_yes() -> None:
    tool = FakeTool(result=FakeResult(True, output="wrote"))
    runtime = _runtime_with({"write_file": tool})
    outcome = asyncio.run(runtime.invoke_tool("write_file", {"file_path": "x"}, allow_writes=True))
    assert outcome.ok and outcome.output == "wrote"


def test_runtime_invoke_tool_before_start_is_not_found() -> None:
    runtime = RealRuntime()
    outcome = asyncio.run(runtime.invoke_tool("read_file", {}))
    assert outcome.found is False
    assert "starting" in outcome.error


def test_runtime_describe_tools_before_start_is_empty() -> None:
    assert asyncio.run(RealRuntime().describe_tools()) == ()
