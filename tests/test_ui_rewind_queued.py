"""Tests for ui/queued_strip.py — queued-next-message strip (DESIGN-SPEC §5)."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from amplifier_app_tui.ui.queued_strip import (
    QUEUED_PREVIEW_CHARS,
    RECALL_HINT,
    QueuedStrip,
    queued_text,
)
from amplifier_app_tui.ui.themes import DEFAULT_THEME, register_themes, theme_id


class QueuedHost(App[None]):
    def __init__(self) -> None:
        super().__init__()
        register_themes(self)
        self.theme = theme_id(DEFAULT_THEME)
        self.recalls = 0

    def compose(self) -> ComposeResult:
        yield QueuedStrip()

    def on_queued_strip_recall_requested(self, message: QueuedStrip.RecallRequested) -> None:
        message.stop()
        self.recalls += 1


def test_queued_text_exact_string() -> None:
    assert (
        queued_text("also update the changelog")
        == '▹ queued next: "also update the changelog" · runs when this turn ends'
        f" · {RECALL_HINT}"
    )


def test_queued_text_has_bounded_single_line_preview() -> None:
    full = "\n".join("x" * 80 for _ in range(20))
    rendered = queued_text(full)
    preview = rendered.split('"', 2)[1]

    assert "\n" not in rendered
    assert len(preview) == QUEUED_PREVIEW_CHARS
    assert preview.endswith("…")
    assert full not in rendered


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
            f'▹ queued next: "also update the changelog" · runs when this turn ends · {RECALL_HINT}'
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


@pytest.mark.asyncio
async def test_click_requests_recall() -> None:
    app = QueuedHost()
    async with app.run_test() as pilot:
        strip = app.query_one(QueuedStrip)
        strip.show_queued("steer with this")
        await pilot.pause()
        await pilot.click(QueuedStrip)
        await pilot.pause()
        assert app.recalls == 1
