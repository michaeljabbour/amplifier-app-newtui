"""Flow tests — the main-chat delegate tail (joint enhancement with the
Rust client's ``test_main_chat_tail_*`` cases in ``src/main.rs``).

While a child lane streams, the MOST recently streaming lane's live tail
— the same ``┆``-guttered, throttled text the lanes panel paints under
the tailed lane's row — also renders in the main transcript under the
working line, prefixed with the lane's short label
(``┆ explorer › I have the big picture…``). It rides LiveTail's lane
mode, so root streams keep preempting it and it clears on lane
completion / turn end exactly like the panel tail. It stays dark while
the lanes panel holds the keyboard or a focused lane fills the screen
with its own transcript.

Driven over the real app (host fan-out included) by feeding normalized
events straight into ``app.reducer`` — the same seam the runtime uses —
so the mid-turn streaming states are deterministic.
"""

from __future__ import annotations

import pytest

from amplifier_app_tui.kernel import events as ev
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


def _delta(app: TuiApp, sub: str, text: str) -> None:
    app.reducer.handle(
        ev.StreamBlockDelta(
            session_id=sub,
            request_id=f"req-{sub}",
            block_index=0,
            block_type="text",
            sequence=0,
            text=text,
        )
    )


@pytest.mark.asyncio
async def test_main_chat_tail_appears_under_working_line_while_child_streams() -> None:
    """Rust: test_main_chat_tail_appears_under_working_line_while_child_streams."""
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        _start_turn(app)
        _spawn(app, CHILD_A, "researcher")
        _delta(app, CHILD_A, "I have the big picture from the README")
        await pilot.pause()

        # The working pulse is up; the delegate tail paints under it via
        # LiveTail's lane mode — exact ┆ + label + text.
        assert blocks_of(app, "working_status")
        assert app.live_tail.lane_mode
        assert (
            app.live_tail.lane_markup
            == "[$dim]┆ researcher › I have the big picture from the README[/]"
        )
        # The bottom-panel tail keeps its unlabeled byte-identical shape.
        assert app.lanes_panel.has_lane_tail


@pytest.mark.asyncio
async def test_main_chat_tail_switches_to_most_recent_streamer() -> None:
    """Rust: test_main_chat_tail_switches_to_most_recent_streamer."""
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        _start_turn(app)
        _spawn(app, CHILD_A, "researcher")
        _spawn(app, CHILD_B, "coder")
        _delta(app, CHILD_A, "scanning provider docs")
        assert app.live_tail.lane_markup == "[$dim]┆ researcher › scanning provider docs[/]"

        # The other lane streams — the main-chat tail follows it.
        _delta(app, CHILD_B, "migrating the store")
        await pilot.pause()
        assert app.live_tail.lane_markup == "[$dim]┆ coder › migrating the store[/]"


@pytest.mark.asyncio
async def test_main_chat_tail_clears_on_lane_completion_and_turn_end() -> None:
    """Rust: test_main_chat_tail_clears_on_lane_completion_and_turn_end."""
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        _start_turn(app)
        _spawn(app, CHILD_A, "researcher")
        _delta(app, CHILD_A, "scanning provider docs")
        assert app.live_tail.lane_mode

        # Lane completion clears the shown tail (same lifecycle as the panel).
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
        assert not app.live_tail.lane_mode
        assert app.live_tail.lane_markup == ""

        # A second streamer, then turn end: the tail never survives the turn.
        _spawn(app, CHILD_B, "coder")
        _delta(app, CHILD_B, "migrating the store")
        assert app.live_tail.lane_mode
        app.reducer.handle(ev.PromptComplete(ts=2.0, session_id=ROOT))
        await pilot.pause()
        assert not app.live_tail.lane_mode
        assert app.live_tail.lane_markup == ""


@pytest.mark.asyncio
async def test_main_chat_tail_absent_when_lanes_panel_focused() -> None:
    """Rust: test_main_chat_tail_absent_when_lanes_panel_focused (adapted:
    the ratatui client has no panel keyboard focus, so its equivalent
    suppression state is a focused lane's transcript filling the screen)."""
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        _start_turn(app)
        _spawn(app, CHILD_A, "researcher")
        _delta(app, CHILD_A, "scanning provider docs")
        assert app.live_tail.lane_mode

        # The panel auto-opened unfocused at fan-out; ctrl-t twice gives it
        # the keyboard. Taking focus retires the painted main-chat tail…
        assert app.lanes_panel.display and not app.lanes_panel.has_focus
        await pilot.press("ctrl+t")
        await pilot.press("ctrl+t")
        assert app.lanes_panel.has_focus
        assert not app.live_tail.lane_mode
        assert app.live_tail.lane_markup == ""

        # …and new child deltas stay dark while the panel holds it (the
        # panel's own ┆ tail under the lane row is the one on show).
        _delta(app, CHILD_A, " — checking trackers next")
        await pilot.pause()
        assert not app.live_tail.lane_mode
        assert app.live_tail.lane_markup == ""
