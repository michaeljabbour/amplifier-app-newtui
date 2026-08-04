"""Tests for ui/sessions_strip.py -- the sessions picker strip (S2 gap 2:
a canonical interactive selection surface for the session table)."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from amplifier_app_tui.kernel.session_manager import SessionSummary
from amplifier_app_tui.ui.sessions_strip import (
    ID_COL_MIN_WIDTH,
    NARROW_ROW_WIDTH,
    SessionsStrip,
    _SessionRow,
    session_row_cells,
)
from amplifier_app_tui.ui.themes import DEFAULT_THEME, register_themes, theme_id

SUMMARIES = (
    SessionSummary(
        session_id="aaaa1111ff", name="auth refactor", bundle="tui", messages=6, turns=3
    ),
    SessionSummary(session_id="bbbb2222ff", name="", bundle="dev", messages=2),
    SessionSummary(session_id="cccc3333ff", state="recovered"),
    SessionSummary(session_id="dddd4444ff", state="corrupt"),
)


class SessionsHost(App[None]):
    """Minimal host app: registers spec themes, records strip messages."""

    def __init__(self) -> None:
        super().__init__()
        register_themes(self)
        self.theme = theme_id(DEFAULT_THEME)
        self.activated: list[str] = []
        self.resumed: list[str] = []
        self.closed = 0

    def compose(self) -> ComposeResult:
        yield SessionsStrip(id="sessions-strip")

    def on_sessions_strip_session_activated(self, message: SessionsStrip.SessionActivated) -> None:
        self.activated.append(message.session_id)

    def on_sessions_strip_resume_requested(self, message: SessionsStrip.ResumeRequested) -> None:
        self.resumed.append(message.session_id)

    def on_sessions_strip_closed(self, message: SessionsStrip.Closed) -> None:
        self.closed += 1


# -- pure helpers -------------------------------------------------------


def test_row_cells_shape_healthy_row() -> None:
    session_id, detail, meta = session_row_cells(SUMMARIES[0], current=False)
    assert session_id == "aaaa1111"
    assert "auth refactor" in detail
    assert "tui" in detail
    assert "6 msgs" in meta


# -- S2 compliance gap 1: Turns in the row -----------------------------------


def test_row_cells_include_turns_at_full_width() -> None:
    """No width (pure-function callers, e.g. tests) or a wide terminal both
    show the full msgs/turns/age form (AC1: name, session, bundle, msgs,
    turns AND age)."""
    _id, _detail, meta = session_row_cells(SUMMARIES[0], current=False, width=None)
    assert "6 msgs" in meta
    assert "3 turns" in meta
    for width in (100, NARROW_ROW_WIDTH, NARROW_ROW_WIDTH + 1):
        _id, _detail, meta = session_row_cells(SUMMARIES[0], current=False, width=width)
        assert "3 turns" in meta, f"width={width}"


def test_row_cells_turns_dash_when_not_recorded() -> None:
    _id, _detail, meta = session_row_cells(SUMMARIES[1], current=False, width=None)
    assert "\u2014 turns" in meta


def test_row_cells_drop_turns_below_narrow_width() -> None:
    """Below :data:`NARROW_ROW_WIDTH` the Turns figure drops out of the meta
    cell -- the pre-existing msgs/age pair stays exactly as it was (S2 gap
    1: keep the row readable at narrow widths, the same width-ladder idea
    the footer already uses)."""
    for width in (1, 20, 40, NARROW_ROW_WIDTH - 1):
        _id, _detail, meta = session_row_cells(SUMMARIES[0], current=False, width=width)
        assert "turns" not in meta, f"width={width}"
        assert "6 msgs" in meta
        assert meta.strip().endswith(SUMMARIES[0].time_ago)


def test_row_cells_show_state_instead_of_name_when_damaged() -> None:
    _id, detail, _meta = session_row_cells(SUMMARIES[2], current=False)
    assert "recovered" in detail
    _id, detail, _meta = session_row_cells(SUMMARIES[3], current=False)
    assert "corrupt" in detail


def test_id_col_min_width_fits_the_short_id() -> None:
    assert ID_COL_MIN_WIDTH >= 8  # short_id is always 8 chars


# -- widget behavior ------------------------------------------------------


@pytest.mark.asyncio
async def test_show_sessions_opens_strip_with_rows() -> None:
    app = SessionsHost()
    async with app.run_test() as pilot:
        strip = app.query_one(SessionsStrip)
        assert not strip.is_open
        strip.show_sessions(SUMMARIES, current="aaaa1111")
        await pilot.pause()
        assert strip.is_open
        assert len(list(strip.query(_SessionRow))) == len(SUMMARIES)
        assert strip.selected_summary == SUMMARIES[0]


@pytest.mark.asyncio
async def test_empty_summaries_keep_strip_closed() -> None:
    app = SessionsHost()
    async with app.run_test() as pilot:
        strip = app.query_one(SessionsStrip)
        strip.show_sessions((), current="")
        await pilot.pause()
        assert not strip.is_open


@pytest.mark.asyncio
async def test_arrow_keys_move_selection_keyboard_parity() -> None:
    """Keyboard parity (S2 gap 2): up/down move the highlighted row."""
    app = SessionsHost()
    async with app.run_test() as pilot:
        strip = app.query_one(SessionsStrip)
        strip.show_sessions(SUMMARIES, current="")
        await pilot.pause()
        strip.focus()
        await pilot.press("down")
        await pilot.pause()
        assert strip.selected_summary == SUMMARIES[1]
        await pilot.press("down")
        await pilot.press("down")
        await pilot.pause()
        assert strip.selected_summary == SUMMARIES[3]
        # Clamped at the end -- no wrap-around.
        await pilot.press("down")
        await pilot.pause()
        assert strip.selected_summary == SUMMARIES[3]
        await pilot.press("up")
        await pilot.pause()
        assert strip.selected_summary == SUMMARIES[2]


@pytest.mark.asyncio
async def test_enter_activates_the_selected_row() -> None:
    """Keyboard parity (S2 gap 2): Enter activates the highlighted row."""
    app = SessionsHost()
    async with app.run_test() as pilot:
        strip = app.query_one(SessionsStrip)
        strip.show_sessions(SUMMARIES, current="")
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        assert app.activated == ["bbbb2222ff"]


@pytest.mark.asyncio
async def test_click_activates_any_row_mouse_parity() -> None:
    """Mouse parity (S2 gap 2): clicking a row activates it directly, no
    separate select-then-activate step (mirrors PaletteStrip)."""
    app = SessionsHost()
    async with app.run_test() as pilot:
        strip = app.query_one(SessionsStrip)
        strip.show_sessions(SUMMARIES, current="")
        await pilot.pause()
        await pilot.click("#sessions-row-2")
        await pilot.pause()
        assert app.activated == ["cccc3333ff"]


@pytest.mark.asyncio
async def test_close_strip_posts_closed_and_hides() -> None:
    app = SessionsHost()
    async with app.run_test() as pilot:
        strip = app.query_one(SessionsStrip)
        strip.show_sessions(SUMMARIES, current="")
        await pilot.pause()
        strip.close_strip()
        await pilot.pause()
        assert not strip.is_open
        assert app.closed == 1


@pytest.mark.asyncio
async def test_current_session_row_is_marked() -> None:
    app = SessionsHost()
    async with app.run_test() as pilot:
        strip = app.query_one(SessionsStrip)
        strip.show_sessions(SUMMARIES, current="aaaa1111")
        await pilot.pause()
        rows = list(strip.query(_SessionRow))
        assert rows[0].current is True
        assert all(not row.current for row in rows[1:])


# -- S2 compliance gap 2: keyboard/mouse resume ------------------------------


@pytest.mark.asyncio
async def test_resume_key_posts_resume_requested_for_selected_row() -> None:
    """Keyboard parity (S2 gap 2): "r" requests resume for the HIGHLIGHTED
    row, mirroring how "enter" activates the highlighted row."""
    app = SessionsHost()
    async with app.run_test() as pilot:
        strip = app.query_one(SessionsStrip)
        strip.show_sessions(SUMMARIES, current="")
        await pilot.pause()
        await pilot.press("down")  # highlight SUMMARIES[1]
        await pilot.press("r")
        await pilot.pause()
        assert app.resumed == ["bbbb2222ff"]
        assert app.activated == []  # "r" must not also activate/open detail


@pytest.mark.asyncio
async def test_click_resume_glyph_posts_resume_requested_mouse_parity() -> None:
    """Mouse parity (S2 gap 2): clicking a row's trailing resume glyph
    requests THAT row directly -- any row, matching activation's own
    "click any row" reach -- without first selecting it via keyboard."""
    app = SessionsHost()
    async with app.run_test() as pilot:
        strip = app.query_one(SessionsStrip)
        strip.show_sessions(SUMMARIES, current="")
        await pilot.pause()
        row = list(strip.query(_SessionRow))[2]
        width = row.size.width
        assert width > 0
        await pilot.click(f"#{row.id}", offset=(width - 1, 0))
        await pilot.pause()
        assert app.resumed == ["cccc3333ff"]
        assert app.activated == []  # the glyph click must NOT also activate


@pytest.mark.asyncio
async def test_click_row_body_still_activates_not_resume() -> None:
    """Regression guard: the new trailing resume glyph must not steal
    clicks from the rest of the row -- both actions keep their own mouse
    reach (S2 requirement: keyboard AND mouse parity for anything added)."""
    app = SessionsHost()
    async with app.run_test() as pilot:
        strip = app.query_one(SessionsStrip)
        strip.show_sessions(SUMMARIES, current="")
        await pilot.pause()
        await pilot.click("#sessions-row-0")
        await pilot.pause()
        assert app.activated == ["aaaa1111ff"]
        assert app.resumed == []
