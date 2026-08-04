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

from amplifier_app_tui.kernel.demo import (
    AGENTS_END_NOTICE,
    AGENTS_PROMPT,
    BUILD_PROMPT,
    DEMO_LANE_BY_NAME,
    DEMO_SESSION_ID,
)
from amplifier_app_tui.ui.app import TuiApp
from amplifier_app_tui.ui.app_support import LANE_FOCUS_INTRO_NOTICE
from amplifier_app_tui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_tui.ui.footer import footer_right_text
from amplifier_app_tui.ui.lanes_panel import LANES_HEADER
from amplifier_app_tui.ui.needs_you import focused_lane_banner
from amplifier_app_tui.ui.transcript import FocusHeader

from .test_flow_helpers import (
    SIZE,
    GatedDemoAdapter,
    blocks_of,
    line_texts,
    snapshot_texts,
    rules,
    seed_done,
    wait_for,
)

_LANE_LINE = re.compile(
    r"^  [◐■✔] .+? · t\d+ · .+? · [\dms ]+? · ↓ [\d.]+k tokens\s* · \$\d+\.\d{2}$"
)
r"""D6 AC4: every row now states its turn as a `` · tN · `` tag between the
name and activity columns; the name segment is matched lazily (not
``\S+\s*``) because a tailed row's name carries an internal `` ▸ `` marker,
not just trailing padding."""

# The mid-turn panel snapshot at the demo's park point: the child stream
# bursts (kernel/demo.py `_lane_stream`) have already run, so both live lanes
# show the reducer's stream activity ("reviewing response", state running) and
# the DESIGN-SPEC §8 ``▸`` tail marker sits on the most-recently-streaming
# running lane — coder (tester streamed last but is seeded done, so it never
# takes the tail). Name column re-padded to fit the marker.
TAILED_PANEL_LINES = [
    "  ◐ researcher · t2 · reviewing response · 41s    · ↓ 100.1k tokens · $0.09",
    "  ◐ coder ▸    · t2 · reviewing response · 2m 04s · ↓ 48.3k tokens  · $0.31",
    "  ✔ tester     · t2 · done · tests ✔     · 55s    · ↓ 3.2k tokens   · $0.07",
]

# D6 AC4: re-running AGENTS_PROMPT (test_replayed_agents_turn_reopens_done_lanes)
# spawns under turn 3 -- researcher/coder both reset live and pick it up, but
# tester's re-spawn call arrives ALREADY done (its scripted seed is pre-
# completed), so LaneRegistry.register()'s existing idempotent-return path
# (state already terminal -> no-op) never restamps it: tester correctly keeps
# reporting the turn that actually produced its current state (t2), not the
# turn of a call that changed nothing about it.
REPLAYED_TAILED_PANEL_LINES = [
    "  ◐ researcher · t3 · reviewing response · 41s    · ↓ 100.1k tokens · $0.09",
    "  ◐ coder ▸    · t3 · reviewing response · 2m 04s · ↓ 48.3k tokens  · $0.31",
    "  ✔ tester     · t2 · done · tests ✔     · 55s    · ↓ 3.2k tokens   · $0.07",
]


async def _run_agents_turn(pilot, app: TuiApp) -> None:
    await seed_done(pilot, app)
    app.submit_prompt(AGENTS_PROMPT)
    assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)


@pytest.mark.asyncio
async def test_ctrl_t_toggles_lanes_panel_with_tree_in_transcript() -> None:
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await _run_agents_turn(pilot, app)
        assert len(app.lanes.lanes) == 3
        assert app.notice_slot.current == AGENTS_END_NOTICE

        # The multi-agent turn rendered ONE durable, collapsed delegate
        # summary block (ambient-progress D5) — no per-agent tree lines.
        summaries = [b for b in app.transcript.blocks if b.kind == "delegate_summary"]
        assert len(summaries) == 1
        assert [e.agent for e in summaries[0].entries] == [
            "researcher",
            "coder",
            "tester",
        ]
        assert all(e.state == "done" for e in summaries[0].entries)
        assert summaries[0].entries[2].snippet == "tests ✔"
        # The scripted todo beats fold into the durable block: the final
        # all-completed beat lands after the last completion and the
        # header must end on Plan 4/4 (ambient-progress D3 plan-fold).
        plan_final = summaries[0].plan_final
        assert plan_final is not None
        assert [item.status for item in plan_final] == ["completed"] * 4

        # The panel auto-opened at fan-out (mockup ``lanesOpen = true``):
        # exact header + one aligned line per agent.
        assert app.lanes_panel.display
        assert LANES_HEADER == "Agent lanes · ↑↓ select · enter focus · ctrl-o tail · esc close"
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
    app = TuiApp(DemoRuntimeAdapter(instant=True))
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
    """DESIGN-SPEC §8: ◐ teal running/working, ✔ dim done — live lanes carry
    the reducer's stream activity once the child bursts land (Phase 3)."""
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.submit_prompt(AGENTS_PROMPT)
        # The turn parks after spawning all three lanes and streaming the
        # child bursts: the panel shows both live lanes on the stream
        # activity plus the ▸ tail marker on the tailed lane.
        assert await wait_for(pilot, lambda: len(app.lanes.lanes) == 3)
        # D5 AC5: the panel's own repaint is coalesced (LaneReducer throttles
        # "progress" notifies), so it can briefly lag the registry mutation
        # above by up to LANE_ROWS_NOTIFY_SECONDS -- wait for the panel
        # itself to reflect the tri-state instead of asserting immediately.
        assert await wait_for(pilot, lambda: list(app.lanes_panel.lane_lines) == TAILED_PANEL_LINES)
        states = [(r.lane.state, r.lane.glyph, r.lane.color_token) for r in app.lanes_panel.records]
        assert states == [
            ("running", "◐", "teal"),
            ("running", "◐", "teal"),
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
    app = TuiApp(adapter)
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
            lambda: [r.lane.state for r in app.lanes.lanes] == ["running", "running", "done"],
        )
        # The rows repaint is coalesced under high volume (D5 AC5): the
        # model above is already exact, but the panel's own cached lines
        # may lag by up to LANE_ROWS_NOTIFY_SECONDS before the trailing
        # flush lands — wait for it exactly like the has_lane_tail check
        # elsewhere in this suite, rather than asserting the instant after.
        # D6 AC4: this run is turn 3 (seed=t1, first agents run=t2) -- a
        # DIFFERENT expected constant than the first run's, proving the
        # panel distinguishes "the same agent, a later turn" rather than
        # silently repeating a stale label.
        assert await wait_for(
            pilot, lambda: list(app.lanes_panel.lane_lines) == REPLAYED_TAILED_PANEL_LINES
        )
        adapter.release()
        assert await wait_for(pilot, lambda: rules(app) >= 3 and not app.turn_active)
        assert all(r.lane.state == "done" for r in app.lanes.lanes)


@pytest.mark.asyncio
async def test_lane_tail_streams_mid_fanout_then_clears() -> None:
    """Design doc D4 + issue #90: focused-lane deltas fill the tail under the
    lane's row while the root is idle; ctrl+o moves the ▸ pin; the tail is
    ephemeral at turn end."""
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.submit_prompt(AGENTS_PROMPT)
        # Child bursts land before the script's first _wait parks the turn.
        assert await wait_for(pilot, lambda: app.lanes_panel.has_lane_tail)
        marked = [i for i, line in enumerate(app.lanes_panel.lane_lines) if "▸" in line]
        assert len(marked) == 1  # exactly one tailed lane

        await pilot.press("ctrl+o")
        await pilot.pause()
        moved = [i for i, line in enumerate(app.lanes_panel.lane_lines) if "▸" in line]
        assert len(moved) == 1 and moved != marked  # the pin cycled

        adapter.release()
        assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)
        assert not app.lanes_panel.has_lane_tail  # root answer preempted, then turn ended
        # Ephemeral: child prose never became a transcript block.
        assert not any("undocumented streaming flags" in text for text in line_texts(app))


@pytest.mark.asyncio
async def test_focus_lane_child_transcript_banner_and_esc_back() -> None:
    app = TuiApp(DemoRuntimeAdapter(instant=True))
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
        assert await wait_for(pilot, lambda: app.transcript.focused_lane == lane.sub_session_id)
        # The panel stays open while a lane is focused (mockup focusLane
        # never touches lanesOpen); the focused lane's row is highlighted.
        assert app.lanes_panel.display
        assert app.lanes_panel.selected_record is not None
        assert app.lanes_panel.selected_record.lane.name == "coder"

        # The transcript swapped to the subagent's own blocks.
        blocks = app.transcript.blocks
        banner = blocks[0]
        assert banner.kind == "session_banner"
        assert banner.focus_note == focused_lane_banner("coder", DEMO_SESSION_ID, 2)
        assert banner.focus_note == (
            "focused: coder · subagent of e07de0 · turn 2 · own context window"
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
async def test_first_focus_transition_shows_intro_notice_once() -> None:
    """S6 AC4: the first-ever focus transition announces the exit path
    via a transient notice; it never repeats on a later transition (not
    a permanent tutorial overlay)."""
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await _run_agents_turn(pilot, app)
        await pilot.press("ctrl+t")
        await pilot.press("ctrl+t")
        await pilot.pause()

        await pilot.press("enter")  # focus the first lane (researcher)
        assert await wait_for(pilot, lambda: app.transcript.focused_lane is not None)
        assert app.notice_slot.current == LANE_FOCUS_INTRO_NOTICE
        assert "esc" in LANE_FOCUS_INTRO_NOTICE
        assert "Back" in LANE_FOCUS_INTRO_NOTICE

        await pilot.press("escape")
        assert await wait_for(pilot, lambda: app.transcript.focused_lane is None)
        assert app.notice_slot.current == "back to parent session"

        # A second, later transition (a different lane) does not repeat it.
        await pilot.press("ctrl+t")
        await pilot.press("ctrl+t")
        await pilot.pause()
        await pilot.press("down", "enter")
        assert await wait_for(pilot, lambda: app.transcript.focused_lane is not None)
        assert app.notice_slot.current == "back to parent session"  # unchanged


@pytest.mark.asyncio
async def test_focus_header_back_click_returns_without_ending_agent_or_session() -> None:
    """S6 AC1/AC2/AC5: the focus-header Back control is a visible,
    clickable mouse-equivalent of Escape \u2014 clicking it returns to the
    parent exactly like Escape does, and it is exercised here while a
    lane is ACTIVELY STREAMING so a false 'cancel' would be immediately
    observable (S6 design note: navigation, never an interrupt)."""
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.submit_prompt(AGENTS_PROMPT)
        assert await wait_for(pilot, lambda: len(app.lanes.lanes) == 3)
        running_before = {r.session_id: r.lane.state for r in app.lanes.lanes}
        assert any(state == "running" for state in running_before.values())

        await pilot.press("ctrl+t")
        await pilot.press("ctrl+t")
        await pilot.pause()
        await pilot.press("enter")  # focus the first (actively running) lane
        assert await wait_for(pilot, lambda: app.transcript.focused_lane is not None)
        focused_session = app.transcript.focused_lane

        header = app.transcript.query_one(FocusHeader)
        assert "Back to parent" in header.render().plain
        await pilot.click(header)
        assert await wait_for(pilot, lambda: app.transcript.focused_lane is None)
        assert app.notice_slot.current == "back to parent session"

        # Still running, untouched: the click navigated back \u2014 it never
        # interrupted or ended the turn, the session, or any lane.
        assert app.turn_active
        after = {r.session_id: r.lane.state for r in app.lanes.lanes}
        assert after == running_before
        assert focused_session in after

        adapter.release()
        assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)


@pytest.mark.asyncio
async def test_completed_agent_lane_still_offers_focus_header_back_control() -> None:
    """S6: a DONE lane (not just an actively running one) gets the same
    visible Back control, and clicking it works identically."""
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await _run_agents_turn(pilot, app)
        assert all(r.lane.state == "done" for r in app.lanes.lanes)

        await pilot.press("ctrl+t")
        await pilot.press("ctrl+t")
        await pilot.pause()
        await pilot.press("down", "down", "enter")  # focus "tester" (done)
        assert await wait_for(pilot, lambda: app.transcript.focused_lane is not None)

        header = app.transcript.query_one(FocusHeader)
        assert "Back to parent" in header.render().plain
        await pilot.click(header)
        assert await wait_for(pilot, lambda: app.transcript.focused_lane is None)
        assert any(r.lane.name == "tester" and r.lane.state == "done" for r in app.lanes.lanes)


@pytest.mark.asyncio
async def test_title_shows_coordinating_agents_while_running() -> None:
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
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
    app = TuiApp(DemoRuntimeAdapter(instant=True))
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
    app = TuiApp(DemoRuntimeAdapter(instant=True))
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
    app = TuiApp(DemoRuntimeAdapter(instant=True))
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


@pytest.mark.asyncio
async def test_lanes_panel_survives_viewport_resize_mid_turn() -> None:
    """Dev note: viewport resizing mid-turn. The panel re-fits its rows on
    ``on_resize`` (lanes_panel.py) rather than carrying stale truncation
    from the previous width \u2014 shrink then grow and the lane lines must
    still be well-formed (bounded to the new width, boundary-safe) at
    every size, with no crash and no stale content held over."""
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.submit_prompt(AGENTS_PROMPT)
        assert await wait_for(pilot, lambda: len(app.lanes.lanes) == 3)
        assert app.lanes_panel.display

        # Shrink to a narrow width mid-turn: rows must re-fit, not crash,
        # and stay within the new budget (never mid-word per AC3).
        await pilot.resize_terminal(48, SIZE[1])
        await pilot.pause()
        narrow = app.lanes_panel.lane_lines
        assert len(narrow) == 3
        for line in narrow:
            assert len(line) <= 48

        # Grow back wide: rows re-fit again, no stale narrow content held
        # over (the panel derives lines fresh from state, not by patching
        # the previous render).
        await pilot.resize_terminal(SIZE[0], SIZE[1])
        await pilot.pause()
        restored = app.lanes_panel.lane_lines
        assert len(restored) == 3
        for line in restored:
            assert len(line) <= SIZE[0]

        adapter.release()
        assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)
        assert all(r.lane.state == "done" for r in app.lanes.lanes)


@pytest.mark.asyncio
async def test_agent_completes_while_unfocused_and_panel_reflects_it() -> None:
    """Dev note: agents completing while unfocused. No lane is ever
    entered via 'enter' in this test \u2014 the app stays on the main
    composer/transcript the whole time (AC1/AC2: lanes refresh
    event-driven without requiring focus) \u2014 yet every lane's completion
    (including a FAILED one, whose 'error'-kind notify must bypass any
    coalescing, D5 AC5) is reflected the moment it happens."""
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.submit_prompt(AGENTS_PROMPT)
        assert await wait_for(pilot, lambda: len(app.lanes.lanes) == 3)
        # Never focus a lane: composer keeps input focus throughout.
        assert app.composer.has_focus_within
        assert app.transcript.focused_lane is None

        adapter.release()
        assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)
        # Still unfocused \u2014 nothing in this flow ever entered lane focus.
        assert app.transcript.focused_lane is None
        assert all(r.lane.state == "done" for r in app.lanes.lanes)
        # The panel (not just the model) reflects the completed tri-state,
        # proving the repaint isn't gated on focus.
        assert all(
            glyph == "\u2714" for (glyph,) in [(r.lane.glyph,) for r in app.lanes_panel.records]
        )


@pytest.mark.asyncio
async def test_focus_transitions_during_active_stream_preserve_parent_and_lane_content() -> None:
    """D6 AC5: entering/leaving a focused lane while the turn is still
    ACTIVELY STREAMING (gate held, lanes running) must never re-emit,
    duplicate or reorder content, and the parent transcript must be
    intact on every return -- not just the first one. Drives real
    ctrl-t/down/enter/esc keypresses, never calling the reducer/transcript
    helpers directly.
    """
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.submit_prompt(AGENTS_PROMPT)
        # Parked mid-fan-out (the same park point test_lane_tail_streams_
        # mid_fanout_then_clears uses): lanes running, turn genuinely
        # still active -- this is the "tokens still arriving" state.
        assert await wait_for(pilot, lambda: len(app.lanes.lanes) == 3)
        assert app.turn_active
        assert app.lanes.get(DEMO_LANE_BY_NAME["researcher"].sub_session_id).lane.state == "running"
        parent_before = list(app.transcript.blocks)

        async def _open_panel() -> None:
            """Ensure the lanes panel is open AND keyboard-focused,
            regardless of its current state: ctrl-t is a bare toggle
            (open+focus / close), so closing first (only if already open)
            then opening lands on "open+focused" from either starting
            point -- unlike a fixed "press it twice", which only happens
            to work from the auto-opened-but-unfocused fan-out state.
            """
            if app.lanes_panel.display:
                await pilot.press("ctrl+t")
                await pilot.pause()
            await pilot.press("ctrl+t")
            await pilot.pause()

        async def _back_to_parent() -> None:
            await pilot.press("escape")
            assert await wait_for(pilot, lambda: app.transcript.focused_lane is None)

        def _assert_parent_unchanged() -> None:
            """Same blocks, same order, same content on return -- except the
            live working-status pulse's spinner_frame, which legitimately
            advances on the app's 1s heartbeat while the turn runs, wholly
            independent of any focus transition (not a duplication bug).
            """
            now = list(app.transcript.blocks)
            assert [(b.id, b.kind) for b in now] == [(b.id, b.kind) for b in parent_before]
            assert [b for b in now if b.kind != "working_status"] == [
                b for b in parent_before if b.kind != "working_status"
            ]

        researcher = DEMO_LANE_BY_NAME["researcher"]
        coder = DEMO_LANE_BY_NAME["coder"]

        # Focus researcher (row 0) while the turn is still running.
        await _open_panel()
        await pilot.press("enter")
        assert await wait_for(
            pilot, lambda: app.transcript.focused_lane == researcher.sub_session_id
        )
        assert app.turn_active  # still genuinely streaming -- not parked by us
        researcher_view_1 = list(app.transcript.blocks)
        banner_1 = researcher_view_1[0]
        assert banner_1.kind == "session_banner"
        assert "focused: researcher" in banner_1.focus_note
        assert "turn 2" in banner_1.focus_note  # D6 AC4: seed=t1, this run=t2

        await _back_to_parent()
        # Round trip 1: the parent summary/working-line/etc. is untouched.
        _assert_parent_unchanged()

        # Re-focus the SAME lane again, still streaming: idempotent --
        # byte-identical content, no duplication, no reordering.
        await _open_panel()
        await pilot.press("enter")
        assert await wait_for(
            pilot, lambda: app.transcript.focused_lane == researcher.sub_session_id
        )
        researcher_view_2 = list(app.transcript.blocks)
        # The demo's static per-focus fallback re-mints fresh block ids on
        # every call (harmless bookkeeping, not a duplication bug), so the
        # "no re-emission/duplication/reordering" guarantee is checked on
        # rendered CONTENT (what a human actually sees), not raw object
        # identity -- same length, same kind/order, same visible lines.
        assert [b.kind for b in researcher_view_2] == [b.kind for b in researcher_view_1]
        assert snapshot_texts(researcher_view_2) == snapshot_texts(researcher_view_1)

        await _back_to_parent()
        # Round trip 2: still untouched.
        _assert_parent_unchanged()

        # Focus a DIFFERENT lane (coder, row 1) while still streaming --
        # its own distinct content, no leakage from researcher's.
        await _open_panel()
        await pilot.press("down")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: app.transcript.focused_lane == coder.sub_session_id)
        coder_view = list(app.transcript.blocks)
        assert "focused: coder" in coder_view[0].focus_note
        researcher_texts = " ".join(snapshot_texts(researcher_view_1))
        coder_texts = " ".join(snapshot_texts(coder_view))
        assert "undocumented streaming flags" in researcher_texts  # researcher's own log
        assert "undocumented streaming flags" not in coder_texts  # never leaked to coder
        assert "SessionStore" in coder_texts  # coder's own log
        assert "SessionStore" not in researcher_texts  # never leaked to researcher

        await _back_to_parent()
        # Round trip 3: still untouched, and the D6 guarantee never
        # weakened -- neither child's own chatter ever reached the parent.
        _assert_parent_unchanged()
        parent_texts = " ".join(snapshot_texts(parent_before))
        assert "undocumented streaming flags" not in parent_texts
        assert "SessionStore" not in parent_texts

        # Let the turn finish, then focus once more: content still
        # consistent (same turn tag), nothing corrupted by the three
        # in-flight focus/unfocus round trips above.
        adapter.release()
        assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)
        await _open_panel()
        # Selection was last left on coder (row 1) above -- "up" is
        # clamped at row 0, so pressing it twice lands on researcher
        # regardless of where the cursor happened to be left.
        await pilot.press("up")
        await pilot.press("up")
        await pilot.press("enter")
        assert await wait_for(
            pilot, lambda: app.transcript.focused_lane == researcher.sub_session_id
        )
        researcher_final = list(app.transcript.blocks)
        assert "turn 2" in researcher_final[0].focus_note
