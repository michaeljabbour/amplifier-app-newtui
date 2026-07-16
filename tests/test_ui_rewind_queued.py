"""Tests for ui/queued_strip.py — queued-next-message strip (DESIGN-SPEC §5)."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from amplifier_app_newtui.ui.queued_strip import QueuedStrip, queued_text
from amplifier_app_newtui.ui.themes import DEFAULT_THEME, register_themes, theme_id


class QueuedHost(App[None]):
    def __init__(self) -> None:
        super().__init__()
        register_themes(self)
        self.theme = theme_id(DEFAULT_THEME)

    def compose(self) -> ComposeResult:
        yield QueuedStrip()


def test_queued_text_exact_string() -> None:
    assert (
        queued_text("also update the changelog")
        == '▹ queued next: "also update the changelog" · runs when this turn ends'
    )


@pytest.mark.asyncio
async def test_hidden_until_message_queued() -> None:
    app = QueuedHost()
    async with app.run_test() as pilot:
        strip = app.query_one(QueuedStrip)
        await pilot.pause()
        assert not strip.display
        assert strip.queued is None
        assert strip.text == ""


@pytest.mark.asyncio
async def test_show_queued_displays_exact_line() -> None:
    app = QueuedHost()
    async with app.run_test() as pilot:
        strip = app.query_one(QueuedStrip)
        strip.show_queued("also update the changelog")
        await pilot.pause()
        assert strip.display
        assert strip.queued == "also update the changelog"
        assert strip.text == (
            '▹ queued next: "also update the changelog" · runs when this turn ends'
        )


@pytest.mark.asyncio
async def test_clear_queued_hides_strip() -> None:
    app = QueuedHost()
    async with app.run_test() as pilot:
        strip = app.query_one(QueuedStrip)
        strip.show_queued("ship it")
        await pilot.pause()
        strip.clear_queued()
        await pilot.pause()
        assert not strip.display
        assert strip.queued is None
        assert strip.text == ""
