"""Real-runtime focused-lane transcripts (DESIGN-SPEC §8).

Child events are diverted from the root transcript (foreign-turn rule)
and must accumulate into a per-lane block list the app can replay on
lane focus — previously only the demo adapter's scripted ``lane_blocks``
could answer, so every real lane focus showed "no transcript for lane".
"""

from __future__ import annotations

from amplifier_app_tui.kernel import events as ev
from amplifier_app_tui.model.blocks import (
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
    reducer.handle(
        ev.AgentCompleted(
            **_env(2.5),
            agent="researcher",
            sub_session_id="s1",
            parent_session_id=SID,
            success=True,
            result="first pass done",
        )
    )
    reducer.handle(ev.PromptComplete(**_env(3.0), response="first pass done"))
    # Replayed turn reuses the sub-session id (the lanes.register reopen
    # rule) — after the prior prompt has closed and its pre-prompt
    # checkpoint has been finalized, the focus transcript must restart.
    _start_and_delegate(reducer, "researcher", "s1", "second brief")
    blocks = reducer.lane_transcript("s1")
    assert blocks is not None
    assert "old work" not in " ".join(_texts(blocks))
    briefs = [b for b in blocks if isinstance(b, UserLine)]
    assert [b.text for b in briefs] == ["second brief"]


def test_lane_transcript_is_bounded_and_keeps_the_seed_rows() -> None:
    from amplifier_app_tui.ui.reducer import _LANE_TRANSCRIPT_MAX_BLOCKS

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


def test_focus_reads_across_growing_stream_never_duplicate_or_reorder() -> None:
    """D6 AC5: "entering/leaving a focused lane while tokens are still
    arriving" at the accumulation layer that feeds every focus read --
    repeated ``lane_transcript`` reads (the same call app.py's focus/
    re-focus path makes) interleaved with genuinely NEW child events
    landing in between must never re-emit, duplicate or reorder what a
    PRIOR read already saw: each read is a byte-for-byte prefix-preserving
    extension of the last, and the D6 foreign-turn guarantee holds
    throughout (none of it ever reaches the root transcript).
    """
    reducer, host = make_reducer()
    _start_and_delegate(reducer, "researcher", "s1", "find the flaky tests")

    # Focus #1: nothing has streamed yet -- just the seed rows.
    snap_1 = reducer.lane_transcript("s1")
    assert snap_1 is not None
    assert len(snap_1) == 2  # banner + delegated brief

    # Tokens keep arriving while the supervisor is elsewhere ("unfocused").
    reducer.handle(
        ev.ContentBlockEnd(
            **_child_env("s1", 2.0), block_type="text", block={"text": "Scanning CI history."}
        )
    )
    # Focus #2: the new prose landed -- snap_1's rows are UNCHANGED, in the
    # SAME order, and the new content appears exactly once.
    snap_2 = reducer.lane_transcript("s1")
    assert snap_2 is not None
    assert snap_2[: len(snap_1)] == snap_1
    assert len(snap_2) == len(snap_1) + 1
    assert _texts(snap_2).count("Scanning CI history.") == 1

    # More tool activity + prose arrive between focus reads.
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
        ev.ContentBlockEnd(
            **_child_env("s1", 3.5), block_type="text", block={"text": "3 flaky tests found."}
        )
    )
    # Focus #3: same prefix-preservation guarantee, now with two more rows.
    snap_3 = reducer.lane_transcript("s1")
    assert snap_3 is not None
    assert snap_3[: len(snap_2)] == snap_2
    assert len(snap_3) == len(snap_2) + 2

    # Completion lands; the recap is appended exactly once, everything
    # earlier is still exactly where it was.
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
    snap_4 = reducer.lane_transcript("s1")
    assert snap_4 is not None
    assert snap_4[: len(snap_3)] == snap_3
    assert len(snap_4) == len(snap_3) + 1

    # Re-reading the SAME settled state repeatedly (a supervisor bouncing
    # focus in and out after completion) is fully idempotent.
    assert reducer.lane_transcript("s1") == snap_4
    assert reducer.lane_transcript("s1") == snap_4

    # D6's own guarantee still holds throughout: none of the child's
    # thinking/prose ever reached the root/main chat.
    assert "Scanning CI history" not in " ".join(_texts(host.blocks))
    assert "3 flaky tests found." not in " ".join(_texts(host.blocks))
