"""Flow tests — delegate streams route to lanes ONLY; the chat carries
compact lifecycle markers.

The old main-chat delegate tail mirrored the tailed lane's live stream
under the working line, so a child agent's thinking/narration painted
BOTH in the lanes panel and in the main transcript (user report: child
reasoning duplicated into the chat). Routing now:

- child stream deltas feed the lanes panel's ┆ tail only — the main
  transcript's LiveTail never enters a lane mode;
- child thinking / prose / tool chatter never become chat blocks (the
  foreign-turn divert, unchanged);
- the chat instead gets one dim ✳ marker per delegate lifecycle beat —
  ``<agent> started · <brief>`` / ``<agent> done · <hint>`` /
  ``<agent> failed · <reason>`` — the orchestrator's view of cross-agent
  activity, without duplicating lane content;
- root-session streaming/thinking renders exactly as before.

Driven over the real app (host fan-out included) by feeding normalized
events straight into ``app.reducer`` — the same seam the runtime uses —
so the mid-turn streaming states are deterministic.
"""

from __future__ import annotations

import pytest

from amplifier_app_tui.kernel import events as ev
from amplifier_app_tui.model.blocks import Answer, Thinking
from amplifier_app_tui.ui.app import TuiApp
from amplifier_app_tui.ui.demo_wiring import DemoRuntimeAdapter

from .test_flow_helpers import SIZE, blocks_of, seed_done

ROOT = "root-session"
CHILD_A = "child-aaaaaaaaaaaaaaaa"
CHILD_B = "child-bbbbbbbbbbbbbbbb"


def _start_turn(app: TuiApp) -> None:
    app.reducer.handle(ev.PromptSubmit(prompt="fan out", ts=1.0, session_id=ROOT))


def _spawn(app: TuiApp, sub: str, name: str) -> None:
    app.reducer.handle(
        ev.AgentSpawned(
            session_id=ROOT,
            ts=1.0,
            agent=name,
            sub_session_id=sub,
            parent_session_id=ROOT,
        )
    )


def _delta(app: TuiApp, sub: str, text: str, block_type: str = "text") -> None:
    app.reducer.handle(
        ev.StreamBlockDelta(
            session_id=sub,
            request_id=f"req-{sub}",
            block_index=0,
            block_type=block_type,
            sequence=0,
            text=text,
        )
    )


def _answer_texts(app: TuiApp) -> list[str]:
    return ["".join(s.text for s in block.spans) for block in blocks_of(app, "answer")]


@pytest.mark.asyncio
async def test_root_live_stream_uses_reducer_owned_main_and_turn_label() -> None:
    """D6 AC4 production seam: app → LiveTail carries main + real turn id.

    Run at the narrow golden width so the label cannot depend on the wide
    lane/plan layout. Child identity remains on its lane row/focus banner.
    """

    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=(40, 24)) as pilot:
        await seed_done(pilot, app)
        _start_turn(app)
        producer, turn = app.reducer.root_stream_identity
        assert producer == "main"
        assert turn > 0
        app.reducer.handle(
            ev.StreamBlockStart(
                session_id=ROOT,
                ts=1.1,
                request_id="root-r1",
                block_index=0,
                block_type="text",
            )
        )
        await pilot.pause()

        assert app.live_tail.identity_label == f"main · t{turn}"
        assert f"main · t{turn} · responding" in app.live_tail._reveal_hint()


@pytest.mark.asyncio
async def test_child_stream_paints_lane_tail_only_never_the_main_chat() -> None:
    """A streaming child feeds the lanes panel's ┆ tail; the main
    transcript's LiveTail stays untouched (no lane mode to mirror into)."""
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        _start_turn(app)
        _spawn(app, CHILD_A, "researcher")
        _delta(app, CHILD_A, "I have the big picture from the README")
        await pilot.pause()

        # The working pulse is up; the lanes panel tail carries the stream.
        assert blocks_of(app, "working_status")
        assert app.lanes_panel.has_lane_tail
        # The main-chat LiveTail never mirrors lane content (API removed).
        assert not hasattr(app.live_tail, "lane_mode")
        assert not app.live_tail.source
        # The streamed prose never became a chat block either.
        assert all("big picture" not in text for text in _answer_texts(app))


@pytest.mark.asyncio
async def test_child_thinking_never_creates_chat_blocks_root_thinking_still_does() -> None:
    """Child thinking (deltas AND durable content blocks) stays out of the
    chat transcript; the root session's Thinking block renders as before."""
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        _start_turn(app)
        # Root thinking → one durable collapsed Thinking block (issue #129).
        app.reducer.handle(ev.ContentBlockStart(session_id=ROOT, block_type="thinking", ts=1.1))
        app.reducer.handle(
            ev.ContentBlockEnd(
                session_id=ROOT,
                block_type="thinking",
                block={"thinking": "root reasoning"},
                ts=1.2,
            )
        )
        _spawn(app, CHILD_A, "researcher")
        # Child thinking arrives via BOTH channels in real sessions.
        _delta(app, CHILD_A, "child secret reasoning", block_type="thinking")
        app.reducer.handle(ev.ContentBlockStart(session_id=CHILD_A, block_type="thinking", ts=1.3))
        app.reducer.handle(
            ev.ContentBlockEnd(
                session_id=CHILD_A,
                block_type="thinking",
                block={"thinking": "child secret reasoning"},
                ts=1.4,
            )
        )
        await pilot.pause()

        thinking = blocks_of(app, "thinking")
        assert [block.text for block in thinking if isinstance(block, Thinking)] == [
            "root reasoning"
        ]
        assert all("child secret reasoning" not in t for t in _answer_texts(app))


@pytest.mark.asyncio
async def test_chat_carries_lifecycle_markers_for_spawn_completion_and_failure() -> None:
    """The chat's cross-agent view is one dim ✳ marker per lifecycle beat."""
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        _start_turn(app)
        # The delegate call's instruction becomes the marker's few-word task.
        app.reducer.handle(
            ev.ToolPre(
                session_id=ROOT,
                ts=1.0,
                tool_name="delegate",
                tool_call_id="d1",
                tool_input={"agent": "researcher", "instruction": "scan provider docs"},
            )
        )
        _spawn(app, CHILD_A, "researcher")
        _spawn(app, CHILD_B, "coder")
        app.reducer.handle(
            ev.AgentCompleted(
                session_id=ROOT,
                ts=2.0,
                agent="researcher",
                sub_session_id=CHILD_A,
                parent_session_id=ROOT,
                success=True,
                result="docs scanned",
            )
        )
        app.reducer.handle(
            ev.AgentCompleted(
                session_id=ROOT,
                ts=2.5,
                agent="coder",
                sub_session_id=CHILD_B,
                parent_session_id=ROOT,
                success=False,
                result="migration blew up",
            )
        )
        await pilot.pause()

        texts = _answer_texts(app)
        assert "✳ researcher started · scan provider docs" in texts
        assert "✳ coder started" in texts
        assert "✳ researcher done · docs scanned" in texts
        assert "✳ coder failed · migration blew up" in texts
        # Markers are the dim recap-line shape: non-clickable status lines.
        markers = [
            block
            for block in blocks_of(app, "answer")
            if isinstance(block, Answer) and "".join(s.text for s in block.spans).startswith("✳ ")
        ]
        assert markers and all(not marker.clickable for marker in markers)


@pytest.mark.asyncio
async def test_lane_tail_still_clears_on_lane_completion_and_turn_end() -> None:
    """The lanes-panel tail lifecycle is unchanged by the chat-side rerouting."""
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        _start_turn(app)
        _spawn(app, CHILD_A, "researcher")
        _delta(app, CHILD_A, "scanning provider docs")
        await pilot.pause()
        assert app.lanes_panel.has_lane_tail

        app.reducer.handle(
            ev.AgentCompleted(
                session_id=ROOT,
                agent="researcher",
                sub_session_id=CHILD_A,
                parent_session_id=ROOT,
                success=True,
                result="docs scanned",
            )
        )
        await pilot.pause()
        assert not app.lanes_panel.has_lane_tail

        _spawn(app, CHILD_B, "coder")
        _delta(app, CHILD_B, "migrating the store")
        await pilot.pause()
        assert app.lanes_panel.has_lane_tail
        app.reducer.handle(ev.PromptComplete(ts=2.0, session_id=ROOT))
        await pilot.pause()
        assert not app.lanes_panel.has_lane_tail
