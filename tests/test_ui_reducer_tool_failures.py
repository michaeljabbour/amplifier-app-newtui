"""Root-tool failure rendering and same-turn recovery.

loop-streaming reports ordinary tool failures as ``tool:post`` with
``success=False`` and then gives that result back to the model.  The reducer
must render the failed attempt honestly without mistaking it for the end of the
turn; a later fallback remains part of the same chronological transcript.
"""

from __future__ import annotations

from amplifier_app_tui.kernel import events as ev
from amplifier_app_tui.model.blocks import ToolLine, TurnRule

from .test_ui_reducer_outcomes import FakeHost, make_reducer


def _tool_lines(host: FakeHost) -> list[ToolLine]:
    return [block for block in host.blocks if isinstance(block, ToolLine)]


def test_failed_tool_post_is_durable_and_fallback_continues_same_turn() -> None:
    reducer, host = make_reducer(mode_id="auto")
    reducer.handle(ev.PromptSubmit(session_id="root", prompt="update config", ts=1.0))
    reducer.handle(
        ev.ToolPre(
            session_id="root",
            tool_name="edit_file",
            tool_call_id="edit-1",
            tool_input={"file_path": "/tmp/outside/config.py"},
            ts=2.0,
        )
    )
    reducer.handle(
        ev.ToolPost(
            session_id="root",
            tool_name="edit_file",
            tool_call_id="edit-1",
            result={
                "success": False,
                "error": {"message": "Access denied: outside allowed write paths"},
            },
            ts=3.0,
        )
    )

    (failed,) = _tool_lines(host)
    assert failed.status == "failed"
    assert failed.summary == "edit_file failed · Access denied: outside allowed write paths"
    assert failed.body == (
        "edited config.py",
        "Access denied: outside allowed write paths",
    )
    assert failed.tool_call_ids == ("edit-1",)
    assert not any(isinstance(block, TurnRule) for block in host.blocks)

    # The model's fallback is a normal next tool call in the same turn.  It
    # starts a fresh digest below the failed row instead of retroactively
    # making the failure look successful.
    reducer.handle(
        ev.ToolPre(
            session_id="root",
            tool_name="bash",
            tool_call_id="bash-2",
            tool_input={"command": "python repair_config.py"},
            ts=4.0,
        )
    )
    reducer.handle(
        ev.ToolPost(
            session_id="root",
            tool_name="bash",
            tool_call_id="bash-2",
            result={"success": True, "output": "ok"},
            ts=5.0,
        )
    )

    failed_after, fallback = _tool_lines(host)
    assert failed_after == failed
    assert fallback.status == "completed"
    assert fallback.summary == "Ran 1 shell command"
    assert host.blocks.index(failed_after) < host.blocks.index(fallback)

    reducer.handle(ev.PromptComplete(session_id="root", response="Recovered.", ts=6.0))
    assert any(isinstance(block, TurnRule) for block in host.blocks)
    assert _tool_lines(host)[0] == failed


def test_failed_status_and_string_output_are_also_failure_evidence() -> None:
    reducer, host = make_reducer(mode_id="auto")
    reducer.handle(ev.PromptSubmit(session_id="root", prompt="run it", ts=1.0))
    reducer.handle(
        ev.ToolPre(
            session_id="root",
            tool_name="bash",
            tool_call_id="bash-1",
            tool_input={"command": "deploy"},
            ts=2.0,
        )
    )
    reducer.handle(
        ev.ToolPost(
            session_id="root",
            tool_name="bash",
            tool_call_id="bash-1",
            result={"status": "FAILED", "output": "remote rejected the deployment"},
            ts=3.0,
        )
    )

    (failed,) = _tool_lines(host)
    assert failed.status == "failed"
    assert failed.summary == "bash failed · remote rejected the deployment"
    assert failed.body == ("$ deploy", "remote rejected the deployment")
