"""``tool list`` / ``tool invoke`` CLI contract (scriptable tool invocation).

The command layer (``main.py``) is exercised through a fake ``RealRuntime`` --
the same seam ``test_run_cli`` monkeypatches -- so these tests cover argument
parsing, output formatting, exit codes, and the governance-refusal message
without booting a real session. Kernel behaviour (the trust gate, the
coordinator invocation contract) is covered in ``test_kernel_tool_ops``.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from amplifier_app_newtui.kernel.session_ops import ToolDescriptor, ToolInvocation
from amplifier_app_newtui.main import _parse_tool_args, main


class FakeRuntime:
    last_args: dict | None = None
    last_allow_writes: bool | None = None

    def __init__(self, *, bundle=None) -> None:
        self.bundle_name = bundle or "newtui"

    async def start(self) -> None:
        # Boot noise must land on stderr, never the machine-readable stdout.
        print("module startup diagnostic")

    async def cleanup(self) -> None:
        return None

    async def describe_tools(self) -> tuple[ToolDescriptor, ...]:
        return (
            ToolDescriptor("bash", "Run a shell command", True),
            ToolDescriptor("noexec", "", False),
            ToolDescriptor("read_file", "Read a file from disk", True),
        )

    async def invoke_tool(self, name, args, *, allow_writes=False) -> ToolInvocation:
        type(self).last_args = args
        type(self).last_allow_writes = allow_writes
        if name == "read_file":
            return ToolInvocation(found=True, ok=True, output={"content": "HELLO_MARKER"})
        if name == "echo_str":
            return ToolInvocation(found=True, ok=True, output="plain text out")
        if name == "missing":
            return ToolInvocation(found=False, ok=False, error="no tool named 'missing' is mounted")
        if name == "danger":
            return ToolInvocation(
                found=True,
                ok=False,
                blocked=True,
                capability="exec",
                error="exec requires approval that a one-shot CLI cannot request",
            )
        return ToolInvocation(found=True, ok=False, error="kaboom")


class FailingRuntime(FakeRuntime):
    async def start(self) -> None:
        print("diagnostic before failure")
        raise RuntimeError("no provider configured")


def _patch(monkeypatch, runtime=FakeRuntime) -> None:
    monkeypatch.setattr("amplifier_app_newtui.kernel.runtime.RealRuntime", runtime)


# -- _parse_tool_args (pure) ------------------------------------------------


def test_parse_key_value_json_decodes_scalars() -> None:
    args, error = _parse_tool_args(("limit=5", "flag=true", "name=readme"), None)
    assert error is None
    assert args == {"limit": 5, "flag": True, "name": "readme"}


def test_parse_key_value_keeps_plain_strings() -> None:
    args, error = _parse_tool_args(("file_path=./a b.txt",), None)
    assert error is None and args == {"file_path": "./a b.txt"}


def test_parse_rejects_missing_equals() -> None:
    args, error = _parse_tool_args(("oops",), None)
    assert args == {} and error is not None and "key=value" in error


def test_parse_rejects_empty_key() -> None:
    _args, error = _parse_tool_args(("=value",), None)
    assert error is not None and "key=value" in error


def test_parse_json_object() -> None:
    args, error = _parse_tool_args((), '{"file_path": "x", "n": 2}')
    assert error is None and args == {"file_path": "x", "n": 2}


def test_parse_json_must_be_object() -> None:
    _args, error = _parse_tool_args((), "[1, 2, 3]")
    assert error is not None and "JSON object" in error


def test_parse_json_invalid() -> None:
    _args, error = _parse_tool_args((), "{not json}")
    assert error is not None and "valid JSON" in error


def test_parse_json_and_pairs_are_exclusive() -> None:
    _args, error = _parse_tool_args(("a=1",), "{}")
    assert error is not None and "not both" in error


# -- tool list --------------------------------------------------------------


def test_tool_list_text_shows_mounted_tools(monkeypatch) -> None:
    _patch(monkeypatch)
    result = CliRunner().invoke(main, ["tool", "list"])
    assert result.exit_code == 0
    assert "read_file" in result.stdout
    assert "Run a shell command" in result.stdout
    assert "(not invokable)" in result.stdout
    # Boot diagnostics are kept off the listing stream.
    assert "module startup diagnostic" not in result.stdout


def test_tool_list_json_is_one_document(monkeypatch) -> None:
    _patch(monkeypatch)
    result = CliRunner().invoke(main, ["tool", "list", "--output-format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert payload["bundle"] == "newtui"
    names = [t["name"] for t in payload["tools"]]
    assert names == ["bash", "noexec", "read_file"]
    assert payload["tools"][1]["invokable"] is False


def test_tool_list_boot_error_exits_nonzero(monkeypatch) -> None:
    _patch(monkeypatch, FailingRuntime)
    result = CliRunner().invoke(main, ["tool", "list"])
    assert result.exit_code == 1
    assert "no provider configured" in result.stderr


# -- tool invoke ------------------------------------------------------------


def test_invoke_prints_result_text(monkeypatch) -> None:
    _patch(monkeypatch)
    result = CliRunner().invoke(main, ["tool", "invoke", "read_file", "file_path=README.md"])
    assert result.exit_code == 0
    assert "HELLO_MARKER" in result.stdout
    assert FakeRuntime.last_args == {"file_path": "README.md"}
    assert FakeRuntime.last_allow_writes is False


def test_invoke_prints_plain_string_output(monkeypatch) -> None:
    _patch(monkeypatch)
    result = CliRunner().invoke(main, ["tool", "invoke", "echo_str"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "plain text out"


def test_invoke_json_output(monkeypatch) -> None:
    _patch(monkeypatch)
    result = CliRunner().invoke(
        main, ["tool", "invoke", "read_file", "file_path=x", "--output-format", "json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {
        "status": "success",
        "tool": "read_file",
        "result": {"content": "HELLO_MARKER"},
    }


def test_invoke_unknown_tool_errors_nonzero(monkeypatch) -> None:
    _patch(monkeypatch)
    result = CliRunner().invoke(main, ["tool", "invoke", "missing"])
    assert result.exit_code == 1
    assert "no tool named 'missing'" in result.stderr


def test_invoke_blocked_reports_capability(monkeypatch) -> None:
    _patch(monkeypatch)
    result = CliRunner().invoke(main, ["tool", "invoke", "danger", "command=rm -rf /"])
    assert result.exit_code == 1
    assert "Blocked:" in result.stderr
    assert "capability: exec" in result.stderr


def test_invoke_execution_error_reports(monkeypatch) -> None:
    _patch(monkeypatch)
    result = CliRunner().invoke(main, ["tool", "invoke", "boom"])
    assert result.exit_code == 1
    assert "Error: kaboom" in result.stderr


def test_invoke_yes_flag_elevates_posture(monkeypatch) -> None:
    _patch(monkeypatch)
    result = CliRunner().invoke(main, ["tool", "invoke", "read_file", "file_path=x", "--yes"])
    assert result.exit_code == 0
    assert FakeRuntime.last_allow_writes is True


def test_invoke_json_arg_object(monkeypatch) -> None:
    _patch(monkeypatch)
    result = CliRunner().invoke(
        main, ["tool", "invoke", "read_file", "--json", '{"file_path": "y"}']
    )
    assert result.exit_code == 0
    assert FakeRuntime.last_args == {"file_path": "y"}


def test_invoke_malformed_args_is_usage_error(monkeypatch) -> None:
    _patch(monkeypatch)
    result = CliRunner().invoke(main, ["tool", "invoke", "read_file", "nope"])
    assert result.exit_code == 2
    assert "key=value" in result.stderr


def test_invoke_boot_error_json(monkeypatch) -> None:
    _patch(monkeypatch, FailingRuntime)
    result = CliRunner().invoke(
        main, ["tool", "invoke", "read_file", "file_path=x", "--output-format", "json"]
    )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert payload["tool"] == "read_file"
    assert payload["error_type"] == "RuntimeError"
