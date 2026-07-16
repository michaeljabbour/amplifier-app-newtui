"""Tests for ui/lanes_panel.py — agent lanes strip (DESIGN-SPEC §8)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from textual.app import App, ComposeResult

from amplifier_app_newtui.model.lanes import LaneRecord, LaneState
from amplifier_app_newtui.ui.lanes_panel import (
    LANES_HEADER,
    LanesPanel,
    format_lane_lines,
    lane_elapsed,
)
from amplifier_app_newtui.ui.themes import DEFAULT_THEME, register_themes, theme_id


def _record(session_id: str, name: str, state: str, activity: str, elapsed: float, cost: str) -> LaneRecord:
    return LaneRecord(
        session_id=session_id,
        parent_id="root",
        lane=LaneState.for_state(
            name=name,
            state=state,  # type: ignore[arg-type]
            activity=activity,
            elapsed=elapsed,
            cost=Decimal(cost),
        ),
    )


# The mockup's three demo lanes, verbatim.
RECORDS = (
    _record("s1", "researcher", "running", "scanning provider docs", 41, "0.09"),
    _record("s2", "coder", "working", "migrating store", 124, "0.31"),
    _record("s3", "tester", "done", "done · tests ✔", 55, "0.07"),
)


class LanesHost(App[None]):
    def __init__(self) -> None:
        super().__init__()
        register_themes(self)
        self.theme = theme_id(DEFAULT_THEME)
        self.focused_lanes: list[tuple[str, str]] = []
        self.closed = 0

    def compose(self) -> ComposeResult:
        yield LanesPanel()

    def on_lanes_panel_focus_lane(self, message: LanesPanel.FocusLane) -> None:
        self.focused_lanes.append((message.name, message.session_id))

    def on_lanes_panel_closed(self, message: LanesPanel.Closed) -> None:
        self.closed += 1


# -- pure formatting -----------------------------------------------------


def test_header_exact_string() -> None:
    assert LANES_HEADER == "Agent lanes · ↑↓ select · enter focus · esc close"


def test_lane_elapsed_format() -> None:
    assert lane_elapsed(41) == "41s"
    assert lane_elapsed(55) == "55s"
    assert lane_elapsed(124) == "2m"
    assert lane_elapsed(0) == "0s"


def test_lane_lines_align_exactly_like_mockup() -> None:
    lines = format_lane_lines(tuple(r.lane for r in RECORDS))
    assert lines == (
        "  ◐ researcher · scanning provider docs · 41s · $0.09",
        "  ■ coder      · migrating store        · 2m  · $0.31",
        "  ✔ tester     · done · tests ✔         · 55s · $0.07",
    )


def test_lane_glyphs_and_colors_per_state() -> None:
    running, working, done = (r.lane for r in RECORDS)
    assert (running.glyph, running.color_token) == ("◐", "teal")
    assert (working.glyph, working.color_token) == ("■", "fg")
    assert (done.glyph, done.color_token) == ("✔", "dim")


def test_empty_lanes_format_to_nothing() -> None:
    assert format_lane_lines(()) == ()


# -- widget behavior ----------------------------------------------------


@pytest.mark.asyncio
async def test_panel_lists_aligned_lanes_and_selects_first() -> None:
    app = LanesHost()
    async with app.run_test() as pilot:
        panel = app.query_one(LanesPanel)
        panel.update_lanes(RECORDS)
        panel.show_panel()
        await pilot.pause()
        assert panel.display
        assert panel.lane_lines == format_lane_lines(tuple(r.lane for r in RECORDS))
        assert panel.selected_record is RECORDS[0]
        from amplifier_app_newtui.ui.lanes_panel import _LaneRow  # test-only

        rows = list(panel.query(_LaneRow))
        assert [r.line for r in rows] == list(panel.lane_lines)
        assert rows[0].has_class("-selected")


@pytest.mark.asyncio
async def test_arrows_move_selection_and_enter_focuses_lane() -> None:
    app = LanesHost()
    async with app.run_test() as pilot:
        panel = app.query_one(LanesPanel)
        panel.update_lanes(RECORDS)
        panel.show_panel()
        await pilot.pause()
        await pilot.press("down")
        assert panel.selected_record is RECORDS[1]
        await pilot.press("down", "down", "down")  # clamped at the end
        assert panel.selected_record is RECORDS[2]
        await pilot.press("up", "up")
        assert panel.selected_record is RECORDS[0]
        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.focused_lanes == [("coder", "s2")]


@pytest.mark.asyncio
async def test_click_focuses_that_lane() -> None:
    app = LanesHost()
    async with app.run_test(size=(100, 40)) as pilot:
        panel = app.query_one(LanesPanel)
        panel.update_lanes(RECORDS)
        panel.show_panel()
        await pilot.pause()
        await pilot.click("#lane-row-2")
        await pilot.pause()
        assert app.focused_lanes == [("tester", "s3")]


@pytest.mark.asyncio
async def test_close_action_hides_and_posts_closed() -> None:
    # Esc is resolved by the app via keymap.ESC_CHAIN (spec §5) — the panel
    # has no local escape binding; the chain invokes ``action_close``.
    app = LanesHost()
    async with app.run_test() as pilot:
        panel = app.query_one(LanesPanel)
        panel.update_lanes(RECORDS)
        panel.show_panel()
        await pilot.pause()
        panel.action_close()
        await pilot.pause()
        assert app.closed == 1
        assert not panel.display


@pytest.mark.asyncio
async def test_set_focused_snaps_highlight() -> None:
    app = LanesHost()
    async with app.run_test() as pilot:
        panel = app.query_one(LanesPanel)
        panel.update_lanes(RECORDS)
        panel.show_panel()
        await pilot.pause()
        panel.set_focused("tester")
        await pilot.pause()
        assert panel.selected_record is RECORDS[2]
