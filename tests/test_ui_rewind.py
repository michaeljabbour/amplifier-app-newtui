"""Tests for ui/rewind_strip.py — rewind picker strip (DESIGN-SPEC §9)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from textual.app import App, ComposeResult

from amplifier_app_newtui.model.turn import Checkpoint
from amplifier_app_newtui.ui.rewind_strip import (
    CLOSE_HINT,
    FORK_HINT,
    RewindStrip,
    rewind_label,
    rewind_line,
)
from amplifier_app_newtui.ui.themes import DEFAULT_THEME, register_themes, theme_id

CHECKPOINTS = (
    Checkpoint(
        id="t1",
        turn_id=1,
        message_index=4,
        cost_at=Decimal("0.18"),
        label="store refactor · shipped",
    ),
    Checkpoint(
        id="t2",
        turn_id=2,
        message_index=9,
        cost_at=Decimal("0.47"),
        label="auto run · shipped locally",
    ),
    Checkpoint(id="t3", turn_id=3, message_index=13, cost_at=Decimal("1.12"), label="plan ready"),
)


class RewindHost(App[None]):
    def __init__(self) -> None:
        super().__init__()
        register_themes(self)
        self.theme = theme_id(DEFAULT_THEME)
        self.forks: list[str] = []
        self.closed = 0

    def compose(self) -> ComposeResult:
        yield RewindStrip()

    def on_rewind_strip_fork_requested(self, message: RewindStrip.ForkRequested) -> None:
        self.forks.append(message.checkpoint_id)

    def on_rewind_strip_closed(self, message: RewindStrip.Closed) -> None:
        self.closed += 1


# -- pure formatting -----------------------------------------------------


def test_rewind_label_exact_string() -> None:
    assert rewind_label(CHECKPOINTS[2]) == "t3 · $1.12 · plan ready"
    assert rewind_line(CHECKPOINTS[0]) == "rewind › t1 · $0.18 · store refactor · shipped"


def test_hint_strings() -> None:
    assert FORK_HINT == "enter fork"
    assert CLOSE_HINT == "esc close"


# -- widget behavior ----------------------------------------------------


@pytest.mark.asyncio
async def test_opens_on_newest_checkpoint_by_default() -> None:
    app = RewindHost()
    async with app.run_test() as pilot:
        strip = app.query_one(RewindStrip)
        assert not strip.display
        strip.show_checkpoints(CHECKPOINTS)
        await pilot.pause()
        assert strip.display
        assert strip.index == 2
        assert strip.label_text == "rewind › t3 · $1.12 · plan ready"


@pytest.mark.asyncio
async def test_opens_at_clicked_rule_checkpoint() -> None:
    app = RewindHost()
    async with app.run_test() as pilot:
        strip = app.query_one(RewindStrip)
        strip.show_checkpoints(CHECKPOINTS, index=0)
        await pilot.pause()
        assert strip.label_text == "rewind › t1 · $0.18 · store refactor · shipped"


@pytest.mark.asyncio
async def test_arrow_navigation_is_clamped() -> None:
    app = RewindHost()
    async with app.run_test() as pilot:
        strip = app.query_one(RewindStrip)
        strip.show_checkpoints(CHECKPOINTS)
        await pilot.pause()
        await pilot.press("left", "left")
        assert strip.index == 0
        await pilot.press("left")  # clamped at the oldest
        assert strip.index == 0
        await pilot.press("right", "right", "right", "right")  # clamped at newest
        assert strip.index == 2
        assert strip.label_text == "rewind › t3 · $1.12 · plan ready"


@pytest.mark.asyncio
async def test_enter_requests_fork_for_current_checkpoint_and_closes() -> None:
    app = RewindHost()
    async with app.run_test() as pilot:
        strip = app.query_one(RewindStrip)
        strip.show_checkpoints(CHECKPOINTS)
        await pilot.pause()
        await pilot.press("left", "enter")
        await pilot.pause()
        assert app.forks == ["t2"]
        assert not strip.display


@pytest.mark.asyncio
async def test_close_action_posts_closed_and_hides() -> None:
    # Esc is resolved by the app via keymap.ESC_CHAIN (spec §5) — the strip
    # has no local escape binding; the chain invokes ``action_close``.
    app = RewindHost()
    async with app.run_test() as pilot:
        strip = app.query_one(RewindStrip)
        strip.show_checkpoints(CHECKPOINTS)
        await pilot.pause()
        strip.action_close()
        await pilot.pause()
        assert app.closed == 1
        assert not strip.display
        assert app.forks == []


@pytest.mark.asyncio
async def test_click_glyphs_navigate_and_fork_chip_forks() -> None:
    app = RewindHost()
    async with app.run_test(size=(120, 40)) as pilot:
        strip = app.query_one(RewindStrip)
        strip.show_checkpoints(CHECKPOINTS)
        await pilot.pause()
        await pilot.click("#rewind-prev")
        await pilot.pause()
        assert strip.index == 1
        await pilot.click("#rewind-next")
        await pilot.pause()
        assert strip.index == 2
        await pilot.click("#rewind-fork")
        await pilot.pause()
        assert app.forks == ["t3"]
        assert not strip.display


@pytest.mark.asyncio
async def test_empty_checkpoints_keep_strip_hidden() -> None:
    app = RewindHost()
    async with app.run_test() as pilot:
        strip = app.query_one(RewindStrip)
        strip.show_checkpoints(())
        await pilot.pause()
        assert not strip.display
