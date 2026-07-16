"""Per-turn yield evidence from the normalized event stream.

Ported from amplifier-app-cli ``ui/turn_outcomes.py``: the ``tests ✔``
heuristic watches the turn's tool results for test-runner commands
(pytest / npm test / …) and reports whether every one of them succeeded
(exited 0). The RealRuntime feeds every emitted event through
:meth:`TurnYieldTracker.observe` via its bridge tap and resets the
tracker at each ``submit`` — subagent tool results count too, exactly
like the reference implementation's cross-session tool snapshot.

Kernel-pure: consumes typed UIEvents only; no Textual, no amplifier-core.
"""

from __future__ import annotations

from typing import Any

from .events import ToolError, ToolPost, ToolPre, UIEvent

_TEST_MARKERS = ("pytest", "npm test", "uv run pytest", "test runner")

_FAILED_STATUSES = frozenset({"denied", "error", "failed"})


def is_shell_tool_name(name: object) -> bool:
    """Return whether a tool activity represents a real shell command."""
    normalized = str(name).strip().lower().rsplit(":", maxsplit=1)[-1]
    normalized = normalized.replace("-", "_")
    return normalized in {
        "bash",
        "exec",
        "exec_command",
        "run_command",
        "shell",
    } or normalized.endswith(("_bash", "_exec_command", "_shell"))


def _is_test_activity(tool_name: str, command: str) -> bool:
    haystack = f"{tool_name} {command}".lower()
    return any(marker in haystack for marker in _TEST_MARKERS)


def _post_succeeded(result: dict[str, Any]) -> bool:
    """A tool:post counts as success unless its result says otherwise.

    Reference semantics (turn_outcomes.py): tool:post terminal status is
    ``succeeded``, tool:error is ``failed``. The normalized result dict
    additionally carries denial/exit information when the runtime has it.
    """
    status = str(result.get("status", "")).lower()
    if status in _FAILED_STATUSES:
        return False
    for key in ("exit_code", "returncode", "exit_status"):
        code = result.get(key)
        if isinstance(code, int) and not isinstance(code, bool):
            return code == 0
    return True


class TurnYieldTracker:
    """Accumulates one turn's test-run evidence from tool events."""

    def __init__(self) -> None:
        self._test_results: list[bool] = []
        self._pending_commands: dict[str, str] = {}

    def start_turn(self) -> None:
        self._test_results = []
        self._pending_commands = {}

    def observe(self, event: UIEvent) -> None:
        match event:
            case ToolPre():
                # Remember the command so a later tool:error (which carries
                # no input) can still be classified as a failed test run.
                command = str(event.tool_input.get("command", ""))
                if command and event.tool_call_id:
                    self._pending_commands[event.tool_call_id] = command
            case ToolPost():
                command = str(event.tool_input.get("command", "")) or (
                    self._pending_commands.pop(event.tool_call_id, "")
                )
                if _is_test_activity(event.tool_name, command):
                    self._test_results.append(_post_succeeded(event.result))
            case ToolError():
                command = self._pending_commands.pop(event.tool_call_id, "")
                if _is_test_activity(event.tool_name, command):
                    self._test_results.append(False)
            case _:
                pass

    @property
    def tests_ok(self) -> bool | None:
        """True/False when test commands ran this turn; None when none did."""
        if not self._test_results:
            return None
        return all(self._test_results)


__all__ = ["TurnYieldTracker", "is_shell_tool_name"]
