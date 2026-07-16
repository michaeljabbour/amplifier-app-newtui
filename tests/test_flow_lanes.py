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

        # The multi-agent turn rendered a live tree that completed to ✔ lines.
        answers = ["".join(s.text for s in b.spans) for b in blocks_of(app, "answer")]
        for lane in DEMO_LANES:
            assert any("├─ " in text and lane.tree_done in text for text in answers)

        # ctrl-t opens the panel: exact header + one aligned line per agent.
        await pilot.press("ctrl+t")
        await pilot.pause()
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

        # ctrl-t again toggles it closed.
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert not app.lanes_panel.display


@pytest.mark.asyncio
async def test_focus_lane_child_transcript_banner_and_esc_back() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await _run_agents_turn(pilot, app)
        await pilot.press("ctrl+t")
        await pilot.pause()

        # ↓ then Enter focuses the second lane (coder).
        await pilot.press("down")
        await pilot.press("enter")
        lane = DEMO_LANE_BY_NAME["coder"]
        assert await wait_for(
            pilot, lambda: app.transcript.focused_lane == lane.sub_session_id
        )
        assert not app.lanes_panel.display

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
        assert await wait_for(pilot, lambda: app.lanes.active_count == 3)
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
        await pilot.press("ctrl+t")
        await pilot.pause()
        await pilot.press("enter")  # focus the first lane (researcher)
        assert await wait_for(pilot, lambda: app.transcript.focused_lane is not None)

        # A turn that needs an approval starts while the lane is focused.
        app.submit_prompt(BUILD_PROMPT)
        assert await wait_for(pilot, lambda: app.approval_bar is not None)
        # Auto-returned to the parent transcript (spec §7).
        assert app.transcript.focused_lane is None
        await pilot.press("enter")  # resolve, let the turn finish
        assert await wait_for(pilot, lambda: not app.turn_active)
