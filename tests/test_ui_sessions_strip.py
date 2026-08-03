"""Tests for ui/sessions_strip.py -- the sessions picker strip (S2 gap 2:
a canonical interactive selection surface for the session table)."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from amplifier_app_tui.kernel.session_manager import SessionSummary
from amplifier_app_tui.ui.sessions_strip import (
    ID_COL_MIN_WIDTH,
    SessionsStrip,
    _SessionRow,
    session_row_cells,
)
from amplifier_app_tui.ui.themes import DEFAULT_THEME, register_themes, theme_id

SUMMARIES = (
    SessionSummary(session_id="aaaa1111ff", name="auth refactor", bundle="tui", messages=6),
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
        self.closed = 0

    def compose(self) -> ComposeResult:
        yield SessionsStrip(id="sessions-strip")

    def on_sessions_strip_session_activated(self, message: SessionsStrip.SessionActivated) -> None:
        self.activated.append(message.session_id)

    def on_sessions_strip_closed(self, message: SessionsStrip.Closed) -> None:
        self.closed += 1


# -- pure helpers -------------------------------------------------------


def test_row_cells_shape_healthy_row() -> None:
    session_id, detail, meta = session_row_cells(SUMMARIES[0], current=False)
    assert session_id == "aaaa1111"
    assert "auth refactor" in detail
    assert "tui" in detail
    assert "6 msgs" in meta


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
