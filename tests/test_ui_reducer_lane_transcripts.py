"""Real-runtime focused-lane transcripts (DESIGN-SPEC §8).

Child events are diverted from the root transcript (foreign-turn rule)
and must accumulate into a per-lane block list the app can replay on
lane focus — previously only the demo adapter's scripted ``lane_blocks``
could answer, so every real lane focus showed "no transcript for lane".
"""

from __future__ import annotations

from amplifier_app_newtui.kernel import events as ev
from amplifier_app_newtui.model.blocks import (
    Answer,
    SessionBanner,
    ToolLine,
    TranscriptBlock,
    UserLine,
)

from .test_ui_reducer_delegates import SID, _env, make_reducer


def _child_env(sub: str, ts: float, n: int = 0) -> dict:
    return {"event_id": f"c{ts}-{n}", "session_id": sub, "parent_id": SID, "ts": ts}


def _start_and_delegate(reducer, agent: str, sub: str, brief: str) -> None:
    reducer.handle(ev.PromptSubmit(**_env(0.0), prompt="fan out"))
    reducer.handle(
        ev.ToolPre(
            **_env(0.5),
            tool_name="delegate",
            tool_call_id="d1",
            tool_input={"agent": agent, "instruction": brief},
        )
    )
    reducer.handle(
        ev.AgentSpawned(**_env(1.0), agent=agent, sub_session_id=sub, parent_session_id=SID)
    )


def _texts(blocks: list[TranscriptBlock]) -> list[str]:
    out: list[str] = []
    for block in blocks:
        if isinstance(block, Answer):
            out.append("".join(s.text for s in block.spans))
    return out


def test_child_events_accumulate_a_focus_transcript() -> None:
    reducer, host = make_reducer()
    _start_and_delegate(reducer, "researcher", "s1", "find the flaky tests")
    reducer.handle(
        ev.ContentBlockEnd(
            **_child_env("s1", 2.0),
            block_type="text",
            block={"text": "Scanning CI history for retries."},
        )
    )
    reducer.handle(
        ev.ToolPost(
            **_child_env("s1", 3.0),
            tool_name="read_file",
            tool_call_id="t1",
            tool_input={"path": "ci.log"},
            result={"success": True},
        )
    )
    reducer.handle(
        ev.AgentCompleted(
            **_env(4.0),
            agent="researcher",
            sub_session_id="s1",
            parent_session_id=SID,
            success=True,
            result="3 flaky tests found",
        )
    )

    blocks = reducer.lane_transcript("s1")
    assert blocks is not None
    banner, brief, prose, tool, recap = blocks
    assert isinstance(banner, SessionBanner)
    assert "focused: researcher" in banner.focus_note
    assert SID[:6] in banner.focus_note
    assert isinstance(brief, UserLine)
    assert brief.text == "find the flaky tests"
    assert brief.mode == "delegated"
    assert isinstance(prose, Answer) and not prose.clickable
    assert "Scanning CI history" in "".join(s.text for s in prose.spans)
    assert isinstance(tool, ToolLine) and tool.status == "completed"
    assert tool.tool_call_ids == ("t1",)
    assert isinstance(recap, Answer)
    assert "✳ " in _texts([recap])[0]
    assert "completed · result reported back to parent" in _texts([recap])[0]
    # The foreign-turn rule still holds: none of it reached the root.
    assert "Scanning CI history" not in " ".join(_texts(host.blocks))


def test_lane_transcript_resolves_by_agent_name_and_misses_cleanly() -> None:
    reducer, _host = make_reducer()
    _start_and_delegate(reducer, "modular-builder", "s1", "build the module")
    assert reducer.lane_transcript("modular-builder") is not None
    assert reducer.lane_transcript("s1") is not None
    assert reducer.lane_transcript("nope") is None


def test_failed_tool_error_and_failure_recap_rows() -> None:
    reducer, _host = make_reducer()
    _start_and_delegate(reducer, "debugger", "s1", "fix it")
    reducer.handle(
        ev.ToolPost(
            **_child_env("s1", 2.0),
            tool_name="bash",
            tool_call_id="t1",
            tool_input={"command": "pytest"},
            result={"success": False},
        )
    )
    reducer.handle(
        ev.ToolError(
            **_child_env("s1", 2.5),
            tool_name="read_file",
            tool_call_id="t2",
            error_message="no such file",
        )
    )
    reducer.handle(
        ev.AgentCompleted(
            **_env(3.0),
            agent="debugger",
            sub_session_id="s1",
            parent_session_id=SID,
            success=False,
            result="boom",
        )
    )
    blocks = reducer.lane_transcript("s1")
    assert blocks is not None
    tools = [b for b in blocks if isinstance(b, ToolLine)]
    assert [t.status for t in tools] == ["failed", "failed"]
    assert "no such file" in tools[1].summary
    assert "failed · boom" in _texts(blocks)[-1]


def test_respawn_resets_the_lane_transcript() -> None:
    reducer, _host = make_reducer()
    _start_and_delegate(reducer, "researcher", "s1", "first brief")
    reducer.handle(
        ev.ContentBlockEnd(**_child_env("s1", 2.0), block_type="text", block={"text": "old work"})
    )
    # Replayed turn reuses the sub-session id (the lanes.register reopen
    # rule) — the focus transcript must restart with it.
    _start_and_delegate(reducer, "researcher", "s1", "second brief")
    blocks = reducer.lane_transcript("s1")
    assert blocks is not None
    assert "old work" not in " ".join(_texts(blocks))
    briefs = [b for b in blocks if isinstance(b, UserLine)]
    assert [b.text for b in briefs] == ["second brief"]


def test_lane_transcript_is_bounded_and_keeps_the_seed_rows() -> None:
    from amplifier_app_newtui.ui.reducer import _LANE_TRANSCRIPT_MAX_BLOCKS

    reducer, _host = make_reducer()
    _start_and_delegate(reducer, "researcher", "s1", "the brief")
    for n in range(_LANE_TRANSCRIPT_MAX_BLOCKS + 25):
        reducer.handle(
            ev.ContentBlockEnd(
                **_child_env("s1", 2.0 + n, n), block_type="text", block={"text": f"row {n}"}
            )
        )
    blocks = reducer.lane_transcript("s1")
    assert blocks is not None
    assert len(blocks) <= _LANE_TRANSCRIPT_MAX_BLOCKS
    assert isinstance(blocks[0], SessionBanner)
    assert isinstance(blocks[1], UserLine)  # seed rows survive the trim
    assert f"row {_LANE_TRANSCRIPT_MAX_BLOCKS + 24}" in _texts(blocks)[-1]
