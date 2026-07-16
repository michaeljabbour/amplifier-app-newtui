"""Flow tests — DESIGN-SPEC §8: agent lanes & subagent focus.

End-to-end over DemoRuntime + Pilot: ctrl-t toggling the lanes panel
(exact header + aligned lane lines), the live in-transcript agent tree,
focusing a lane (child transcript + banner + [delegated] brief + footer
hint), esc returning to the parent, the coordinating title, and an
approval arriving while a lane is focused auto-returning to the parent.
"""

from __future__ import annotations

import re

import pytest

from amplifier_app_newtui.kernel.demo import (
    AGENTS_END_NOTICE,
    AGENTS_PROMPT,
    BUILD_PROMPT,
    DEMO_LANE_BY_NAME,
    DEMO_LANES,
    DEMO_SESSION_ID,
)
from amplifier_app_newtui.ui.app import NewTuiApp
from amplifier_app_newtui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_newtui.ui.footer import footer_right_text
from amplifier_app_newtui.ui.lanes_panel import LANES_HEADER
from amplifier_app_newtui.ui.needs_you import focused_lane_banner

from .test_flow_helpers import (
    SIZE,
    GatedDemoAdapter,
    blocks_of,
    rules,
    seed_done,
    wait_for,
)

_LANE_LINE = re.compile(r"^  [◐■✔] \S+\s* · .+? · \S+\s* · \$\d+\.\d{2}$")


async def _run_agents_turn(pilot, app: NewTuiApp) -> None:
    await seed_done(pilot, app)
    app.submit_prompt(AGENTS_PROMPT)
    assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)


@pytest.mark.asyncio
async def test_ctrl_t_toggles_lanes_panel_with_tree_in_transcript() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await _run_agents_turn(pilot, app)
        assert len(app.lanes.lanes) == 3
        assert app.notice_slot.current == AGENTS_END_NOTICE

        # The multi-agent turn rendered a live tree that completed to ✔
        # lines — └─ corner glyph on the last-spawned lane, ├─ above it.
        answers = ["".join(s.text for s in b.spans) for b in blocks_of(app, "answer")]
        for lane, branch in zip(DEMO_LANES, ("├─ ", "├─ ", "└─ "), strict=True):
            assert any(branch in text and lane.tree_done in text for text in answers)

        # The panel auto-opened at fan-out (mockup ``lanesOpen = true``):
        # exact header + one aligned line per agent.
        assert app.lanes_panel.display
        assert LANES_HEADER == "Agent lanes · ↑↓ select · enter focus · esc close"
        lines = app.lanes_panel.lane_lines
        assert len(lines) == 3
        for line in lines:
            assert _LANE_LINE.match(line), line
        assert [r.lane.name for r in app.lanes_panel.records] == [
            "researcher",
            "coder",
            "tester",
        ]

        # ctrl-t toggles it closed, then open again.
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert not app.lanes_panel.display
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert app.lanes_panel.display


@pytest.mark.asyncio
async def test_typing_passes_through_focused_lanes_panel_to_composer() -> None:
    """Mockup keydown (the composer input keeps focus while lanesOpen):
    printable keys typed while the panel holds focus are never swallowed —
    '/' opens the palette and text lands in the composer (type-to-steer)."""
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await _run_agents_turn(pilot, app)
        # Auto-opened at fan-out; ctrl-t twice gives the panel keyboard focus.
        await pilot.press("ctrl+t")
        await pilot.press("ctrl+t")
        assert app.lanes_panel.has_focus

        # '/' reaches the composer and opens the command palette.
        await pilot.press("/")
        assert await wait_for(pilot, lambda: app.palette.is_open)
        assert app.composer.text == "/"
        assert app.composer.has_focus_within
        assert app.lanes_panel.display  # the panel stays open

        # Esc closes the palette first (ESC_CHAIN); reset the input.
        await pilot.press("escape")
        assert await wait_for(pilot, lambda: not app.palette.is_open)
        app.composer.clear()

        # Refocus the panel and type plain text: it lands in the composer.
        await pilot.press("ctrl+t")
        await pilot.press("ctrl+t")
        assert app.lanes_panel.has_focus
        await pilot.press("h", "i")
        assert await wait_for(pilot, lambda: app.composer.text == "hi")
        assert app.composer.has_focus_within

        # ↑↓/enter still belong to the panel when it holds focus.
        app.composer.clear()
        await pilot.press("ctrl+t")
        await pilot.press("ctrl+t")
        await pilot.press("down")
        await pilot.pause()
        record = app.lanes_panel.selected_record
        assert record is not None and record.lane.name == "coder"


@pytest.mark.asyncio
async def test_lanes_panel_tri_state_matches_mockup_mid_turn() -> None:
    """DESIGN-SPEC §8: ◐ teal running, ■ fg working, ✔ dim done (mockup LANES)."""
    adapter = GatedDemoAdapter()
    app = NewTuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.submit_prompt(AGENTS_PROMPT)
        # The turn parks after spawning all three lanes: the panel shows
        # the mockup's tri-state snapshot verbatim.
        assert await wait_for(pilot, lambda: len(app.lanes.lanes) == 3)
        assert list(app.lanes_panel.lane_lines) == [
            lane.panel_line for lane in DEMO_LANES
        ]
        states = [(r.lane.state, r.lane.glyph, r.lane.color_token) for r in app.lanes_panel.records]
        assert states == [
            ("running", "◐", "teal"),
            ("working", "■", "fg"),
            ("done", "✔", "dim"),
        ]
        adapter.release()
        assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)
        assert all(r.lane.state == "done" for r in app.lanes.lanes)


@pytest.mark.asyncio
async def test_replayed_agents_turn_reopens_done_lanes() -> None:
    """DESIGN-SPEC §8: re-running the agents turn reuses sub-session ids
    (demo replay) — the panel must show the live tri-state again, not a
    stale ``✔ … done`` carried over from the first run."""
    adapter = GatedDemoAdapter()
    app = NewTuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.submit_prompt(AGENTS_PROMPT)
        assert await wait_for(pilot, lambda: len(app.lanes.lanes) == 3)
        adapter.release()
        assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)
        assert all(r.lane.state == "done" for r in app.lanes.lanes)

        # Replay: park mid-turn again and check the lanes came back live.
        adapter.gate.clear()
        app.submit_prompt(AGENTS_PROMPT)
        assert await wait_for(
            pilot,
            lambda: [r.lane.state for r in app.lanes.lanes]
            == ["running", "working", "done"],
        )
        assert list(app.lanes_panel.lane_lines) == [
            lane.panel_line for lane in DEMO_LANES
        ]
        adapter.release()
        assert await wait_for(pilot, lambda: rules(app) >= 3 and not app.turn_active)
        assert all(r.lane.state == "done" for r in app.lanes.lanes)


@pytest.mark.asyncio
async def test_focus_lane_child_transcript_banner_and_esc_back() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await _run_agents_turn(pilot, app)
        # The panel auto-opened at fan-out (display only); ctrl-t twice
        # gives it keyboard focus for the ↑↓/enter selection path.
        assert app.lanes_panel.display
        await pilot.press("ctrl+t")
        await pilot.press("ctrl+t")
        await pilot.pause()

        # ↓ then Enter focuses the second lane (coder).
        await pilot.press("down")
        await pilot.press("enter")
        lane = DEMO_LANE_BY_NAME["coder"]
        assert await wait_for(
            pilot, lambda: app.transcript.focused_lane == lane.sub_session_id
        )
        # The panel stays open while a lane is focused (mockup focusLane
        # never touches lanesOpen); the focused lane's row is highlighted.
        assert app.lanes_panel.display
        assert app.lanes_panel.selected_record is not None
        assert app.lanes_panel.selected_record.lane.name == "coder"

        # The transcript swapped to the subagent's own blocks.
        blocks = app.transcript.blocks
        banner = blocks[0]
        assert banner.kind == "session_banner"
        assert banner.focus_note == focused_lane_banner("coder", DEMO_SESSION_ID)
        assert banner.focus_note == (
            "focused: coder · subagent of e07de0 · own context window"
            " · results report back to parent · esc back"
        )
        delegated = blocks[1]
        assert delegated.kind == "user_line"
        assert delegated.mode == "delegated"
        assert delegated.text == lane.brief
        # Its own log rendered (narration/tool/command rows) + state recap.
        assert blocks_of(app, "narration")
        assert blocks[-1].kind == "answer"
        assert lane.state_recap in "".join(s.text for s in blocks[-1].spans)

        # Footer hint while lane-focused (exact spec string).
        assert app.footer_bar.state.context == "lane_focus"
        assert (
            footer_right_text(app.footer_bar.state)
            == "esc back to parent · transcript is the subagent's own"
        )

        # Esc returns to the parent transcript.
        await pilot.press("escape")
        assert await wait_for(pilot, lambda: app.transcript.focused_lane is None)
        assert app.notice_slot.current == "back to parent session"
        assert any(b.text == AGENTS_PROMPT for b in blocks_of(app, "user_line"))


@pytest.mark.asyncio
async def test_title_shows_coordinating_agents_while_running() -> None:
    adapter = GatedDemoAdapter()
    app = NewTuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.submit_prompt(AGENTS_PROMPT)
        # The turn parks after spawning all three lanes.
        assert await wait_for(pilot, lambda: len(app.lanes.lanes) == 3)
        assert app.reducer.title_state() == "✳ coordinating 3 agents"
        assert "✳ coordinating 3 agents" in app.title_bar.title_text()
        adapter.release()
        assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)
        assert app.reducer.title_state() == "ready"


@pytest.mark.asyncio
async def test_approval_arriving_while_lane_focused_returns_to_parent() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await _run_agents_turn(pilot, app)
        # Auto-opened at fan-out; ctrl-t twice gives it keyboard focus.
        await pilot.press("ctrl+t")
        await pilot.press("ctrl+t")
        await pilot.pause()
        await pilot.press("enter")  # focus the first lane (researcher)
        assert await wait_for(pilot, lambda: app.transcript.focused_lane is not None)

        # A turn that needs an approval starts while the lane is focused.
        # (The agents turn left the app in build mode — mockup setMode(3);
        # the pytest ask is chat-mode-only under §4 live trust gating.)
        app.set_mode_by_id("chat", notify=False)
        app.submit_prompt(BUILD_PROMPT)
        assert await wait_for(pilot, lambda: app.approval_bar is not None)
        # Auto-returned to the parent transcript (spec §7) with the
        # mockup's notice (requestApproval, html:298) as the final one.
        assert app.transcript.focused_lane is None
        assert app.notice_slot.current == "back to parent · approval required"
        await pilot.press("enter")  # resolve, let the turn finish
        assert await wait_for(pilot, lambda: not app.turn_active)


@pytest.mark.asyncio
async def test_esc_chain_holds_while_lanes_panel_owns_the_keyboard() -> None:
    """Spec §5 / mockup onKeyDown: Esc order is lane-focus → palette →
    rewind → lanes → interrupt, even while the lanes panel holds focus."""
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)

        # Palette open, then ctrl-t hands the lanes panel keyboard focus.
        await pilot.press("/")
        assert await wait_for(pilot, lambda: app.palette.is_open)
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert app.lanes_panel.display
        await pilot.press("escape")  # palette closes first…
        await pilot.pause()
        assert not app.palette.is_open
        assert app.lanes_panel.display  # …the lanes panel stays open

        # Rewind opens (and takes focus) while the lanes panel is up.
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert app.rewind.display
        await pilot.press("escape")  # rewind closes before lanes…
        await pilot.pause()
        assert not app.rewind.display
        assert app.lanes_panel.display

        await pilot.press("escape")  # …and only now the lanes panel closes
        await pilot.pause()
        assert not app.lanes_panel.display


@pytest.mark.asyncio
async def test_esc_chain_holds_while_palette_strip_owns_the_keyboard() -> None:
    """Spec §5: lane-focus unfocuses before the palette closes, even when
    the palette strip itself holds keyboard focus (e.g. after a click)."""
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await _run_agents_turn(pilot, app)
        # Auto-opened at fan-out; ctrl-t twice gives it keyboard focus.
        await pilot.press("ctrl+t")
        await pilot.press("ctrl+t")
        await pilot.pause()
        await pilot.press("enter")  # focus the first lane (researcher)
        assert await wait_for(pilot, lambda: app.transcript.focused_lane is not None)

        await pilot.press("/")
        assert await wait_for(pilot, lambda: app.palette.is_open)
        app.palette.focus()  # clicking the strip body focuses it
        await pilot.pause()

        await pilot.press("escape")  # lane unfocuses first…
        assert await wait_for(pilot, lambda: app.transcript.focused_lane is None)
        assert app.palette.is_open  # …the palette stays open

        await pilot.press("escape")  # …and only now the palette closes
        assert await wait_for(pilot, lambda: not app.palette.is_open)
