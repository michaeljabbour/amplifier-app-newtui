"""Code Mode ``execute`` client render: the durable program + bridged trace +
result/diagnostics ToolLine (HGT: codemode-execute-client).

The backend surfaces ``execute`` through the generic tool-event plumbing (serve
was not modified). Without a client special-case it would fold into the burst
digest as an opaque ``used execute``; the reducer instead emits one durable,
expandable :class:`ToolLine` that mirrors the donor TUI ``<Execute>`` render.

Offline: fake events straight into the reducer, no Textual. The Rust suite pins
the behavioral equivalent (src/ui/reducer.rs codemode_execute tests).
"""

from __future__ import annotations

from amplifier_app_tui.kernel import events as ev
from amplifier_app_tui.model.blocks import BlockIdAllocator, ToolLine
from amplifier_app_tui.model.lanes import LaneRegistry
from amplifier_app_tui.model.turn import OutcomeLedger
from amplifier_app_tui.ui.reducer import TranscriptReducer, codemode_execute_block

from .test_ui_reducer_outcomes import FakeHost

PROGRAM = (
    "totals = []\nfor path in tools.read.list_files({}):\n    totals.append(path)\nreturn totals"
)


def make_reducer() -> tuple[TranscriptReducer, FakeHost]:
    host = FakeHost()
    reducer = TranscriptReducer(
        host,
        allocator=BlockIdAllocator(),
        ledger=OutcomeLedger(),
        lanes=LaneRegistry(),
    )
    return reducer, host


def _tool_lines(host: FakeHost) -> list[ToolLine]:
    return [b for b in host.blocks if isinstance(b, ToolLine)]


# -- the pure builder --------------------------------------------------------


def test_block_surfaces_program_trace_and_result() -> None:
    block = codemode_execute_block(
        {"code": PROGRAM},
        {
            "output": "scanned 2 files",
            "status": "completed",
            "tool_calls": [
                {"name": "read.list_files", "status": "completed"},
                {"name": "read.read_file", "status": "completed"},
            ],
        },
        block_id="b1",
    )
    body = "\n".join(block.body)
    assert block.summary == "Code Mode · execute · 2 tool calls"
    assert block.status == "completed"
    # program source
    assert "program" in body
    assert "for path in tools.read.list_files({}):" in body
    assert "return totals" in body
    # bridged trace with the ↳ marker
    assert "↳ read.list_files" in body
    assert "↳ read.read_file" in body
    # result
    assert "result" in body
    assert "scanned 2 files" in body


def test_block_flags_failed_child_call() -> None:
    block = codemode_execute_block(
        {"code": "return 1"},
        {
            "output": "ok",
            "tool_calls": [
                {"name": "read.read_file", "status": "completed"},
                {"name": "write.write_file", "status": "error"},
            ],
        },
        block_id="b1",
    )
    body = "\n".join(block.body)
    assert "↳ write.write_file · error" in body
    # a completed call carries no status suffix
    assert "↳ read.read_file\n" in body + "\n"


def test_block_singular_call_label() -> None:
    block = codemode_execute_block(
        {"code": "return 1"},
        {"tool_calls": [{"name": "read.read_file", "status": "completed"}]},
        block_id="b1",
    )
    assert block.summary == "Code Mode · execute · 1 tool call"


def test_block_renders_diagnostic_failure() -> None:
    block = codemode_execute_block(
        {"code": "import os"},
        {
            "ok": False,
            "error": True,
            "diagnostic": {
                "kind": "unsupported_syntax",
                "message": "import is not available in code mode",
                "suggestions": ["use the supplied tools instead"],
            },
        },
        block_id="b1",
    )
    body = "\n".join(block.body)
    assert block.status == "failed"
    assert block.summary.endswith("· failed")
    assert "import is not available in code mode" in body
    assert "use the supplied tools instead" in body


def test_block_tolerates_donor_metadata_shape() -> None:
    """Honest seam: donor packs the trace as ``metadata.toolCalls`` with a
    ``tool`` key rather than ``name``."""
    block = codemode_execute_block(
        {"code": "return 1"},
        {
            "output": "done",
            "metadata": {
                "toolCalls": [
                    {"tool": "fs.read", "status": "completed"},
                    {"tool": "fs.write", "status": "running"},
                ]
            },
        },
        block_id="b1",
    )
    body = "\n".join(block.body)
    assert "↳ fs.read" in body
    assert "↳ fs.write · running" in body
    assert block.summary == "Code Mode · execute · 2 tool calls"


def test_block_bounds_a_huge_program() -> None:
    huge = "\n".join(f"x{i} = {i}" for i in range(500))
    block = codemode_execute_block({"code": huge}, {}, block_id="b1")
    body = "\n".join(block.body)
    assert "more lines" in body


def test_no_calls_reads_gracefully() -> None:
    block = codemode_execute_block({"code": "return 42"}, {"output": "42"}, block_id="b1")
    assert block.summary == "Code Mode · execute · no tool calls"
    assert "tool calls" not in "\n".join(block.body)  # section omitted when empty


# -- the reducer routing -----------------------------------------------------


def test_reducer_special_cases_execute_into_a_codemode_block() -> None:
    reducer, host = make_reducer()
    reducer.handle(ev.PromptSubmit(session_id="root", prompt="orchestrate", ts=1.0))
    reducer.handle(
        ev.ToolPre(
            session_id="root",
            tool_name="execute",
            tool_call_id="c1",
            tool_input={"code": PROGRAM},
            ts=2.0,
        )
    )
    reducer.handle(
        ev.ToolPost(
            session_id="root",
            tool_name="execute",
            tool_call_id="c1",
            tool_input={"code": PROGRAM},
            result={
                "output": "scanned 2 files",
                "status": "completed",
                "tool_calls": [{"name": "read.list_files", "status": "completed"}],
            },
            ts=3.0,
        )
    )
    lines = _tool_lines(host)
    codemode = [b for b in lines if "Code Mode" in b.summary]
    assert len(codemode) == 1
    block = codemode[0]
    assert block.tool_call_ids == ("c1",)
    body = "\n".join(block.body)
    assert "return totals" in body
    assert "↳ read.list_files" in body
    assert "scanned 2 files" in body
    # It is its OWN block, not folded into a generic `used execute` digest.
    assert not any("used execute" in b.summary for b in lines)


def test_reducer_execute_failure_marks_the_block_failed() -> None:
    reducer, host = make_reducer()
    reducer.handle(ev.PromptSubmit(session_id="root", prompt="orchestrate", ts=1.0))
    reducer.handle(
        ev.ToolPre(
            session_id="root",
            tool_name="execute",
            tool_call_id="c1",
            tool_input={"code": "import os"},
            ts=2.0,
        )
    )
    reducer.handle(
        ev.ToolPost(
            session_id="root",
            tool_name="execute",
            tool_call_id="c1",
            tool_input={"code": "import os"},
            result={
                "error": True,
                "diagnostic": {
                    "kind": "unsupported_syntax",
                    "message": "import is not available in code mode",
                },
            },
            ts=3.0,
        )
    )
    codemode = [b for b in _tool_lines(host) if "Code Mode" in b.summary]
    assert len(codemode) == 1
    assert codemode[0].status == "failed"
    assert "import is not available in code mode" in "\n".join(codemode[0].body)
