"""Tests for the footer status bar (ui/footer.py) — exact spec strings."""

from __future__ import annotations

from decimal import Decimal

import pytest
from textual.app import App, ComposeResult
from textual.content import Content
from textual.message import Message
from textual.widgets import Static

from amplifier_app_newtui.ui.footer import (
    FooterBar,
    FooterState,
    footer_left_text,
    footer_right_text,
    footer_waiting_text,
)
from amplifier_app_newtui.ui.themes import DEFAULT_THEME, register_themes, theme_id

FULL_STATE = FooterState(
    mode_id="build",
    bundle="dev-bundle",
    session_short="a1b2c3",
    cost=Decimal("0.87"),
    shipped=True,
    queued=1,
    waiting=0,
    context="idle",
)


# -- pure text builders ---------------------------------------------------------


def test_left_text_full_state_exact() -> None:
    assert footer_left_text(FULL_STATE) == (
        "mode build · auto read,test · ask write,net,spend"
        " · dev-bundle · a1b2c3 · $0.87 ▲ · q1"
    )


def test_left_text_minimal_state() -> None:
    state = FooterState()
    assert footer_left_text(state) == "mode chat · ask all · auto read · $0.00"


def test_left_text_no_yield_no_queue() -> None:
    state = FooterState(mode_id="plan", cost=Decimal("1.24"))
    assert footer_left_text(state) == "mode plan · read-only · $1.24"


def test_waiting_text_singular_plural_empty() -> None:
    assert footer_waiting_text(FooterState(waiting=1)) == "1 decision waiting · ctrl-y"
    assert footer_waiting_text(FooterState(waiting=3)) == "3 decisions waiting · ctrl-y"
    assert footer_waiting_text(FooterState(waiting=0)) == ""


def test_right_hints_exact_per_context() -> None:
    assert footer_right_text(FooterState(context="approval")) == (
        "arrows select · enter confirm · esc deny"
    )
    assert footer_right_text(FooterState(context="lane_focus")) == (
        "esc back to parent · transcript is the subagent's own"
    )
    assert footer_right_text(FooterState(context="palette")) == (
        "↑↓ select · enter run · esc close"
    )
    assert footer_right_text(FooterState(context="running")) == (
        "esc interrupt · enter steer · shift+enter queue"
    )
    assert footer_right_text(FooterState(context="idle")) == (
        "/ commands · shift+tab mode · ctrl-t tasks"
    )


def test_running_hint_swaps_queue_chord_without_kitty() -> None:
    state = FooterState(context="running", kitty_protocol=False)
    assert footer_right_text(state) == "esc interrupt · enter steer · alt+enter queue"


def test_unknown_hint_context_falls_back_to_idle() -> None:
    state = FooterState(context="rewind")
    assert footer_right_text(state) == "/ commands · shift+tab mode · ctrl-t tasks"


# -- widget rendering ---------------------------------------------------------------


class FooterApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        register_themes(self)
        self.theme = theme_id(DEFAULT_THEME)
        self.messages: list[Message] = []

    def compose(self) -> ComposeResult:
        yield FooterBar(id="footer")

    def on_footer_bar_waiting_badge_clicked(
        self, message: FooterBar.WaitingBadgeClicked
    ) -> None:
        self.messages.append(message)


def _plain(widget: Static) -> str:
    content = widget.content
    return getattr(content, "plain", str(content))


@pytest.mark.asyncio
async def test_footer_renders_left_and_right_segments() -> None:
    app = FooterApp()
    async with app.run_test() as pilot:
        bar = app.query_one("#footer", FooterBar)
        bar.update_state(FULL_STATE)
        await pilot.pause()
        assert _plain(app.query_one("#footer-left", Static)) == footer_left_text(
            FULL_STATE
        )
        assert _plain(app.query_one("#footer-right", Static)) == footer_right_text(
            FULL_STATE
        )


@pytest.mark.asyncio
async def test_footer_left_separators_use_dimmer_token() -> None:
    """Mockup footer-left: every inline ``·`` between segments is its own
    ``--dimmer`` span while segment text stays dim (§2)."""
    app = FooterApp()
    async with app.run_test() as pilot:
        bar = app.query_one("#footer", FooterBar)
        bar.update_state(FULL_STATE)
        await pilot.pause()
        content = app.query_one("#footer-left", Static).content
        assert isinstance(content, Content)
        dimmer_runs = [
            content.plain[span.start : span.end]
            for span in content.spans
            if span.style == "$dimmer"
        ]
        # mode·trust, trust·bundle, bundle·session, session·cost = 4 separators
        # (the orange "· q1" queue badge separator is NOT dimmer).
        assert dimmer_runs == [" · "] * 4


@pytest.mark.asyncio
async def test_footer_badge_hidden_when_no_decisions_waiting() -> None:
    app = FooterApp()
    async with app.run_test() as pilot:
        bar = app.query_one("#footer", FooterBar)
        bar.update_state(FooterState(waiting=0))
        await pilot.pause()
        badge = bar._badge
        assert not badge.has_class("-visible")


@pytest.mark.asyncio
async def test_footer_badge_shows_and_click_posts_message() -> None:
    app = FooterApp()
    async with app.run_test() as pilot:
        bar = app.query_one("#footer", FooterBar)
        bar.update_state(FooterState(waiting=2))
        await pilot.pause()
        badge = bar._badge
        assert badge.has_class("-visible")
        assert _plain(badge) == "2 decisions waiting · ctrl-y"
        await pilot.click(badge)
        await pilot.pause()
        assert len(app.messages) == 1
        assert isinstance(app.messages[0], FooterBar.WaitingBadgeClicked)


@pytest.mark.asyncio
async def test_footer_badge_wraps_onto_own_row_at_narrow_width() -> None:
    """Mockup footer has flex-wrap: wrap — when the left segment plus the
    waiting badge exceed the width, the badge drops to its own row (fully
    readable and clickable) instead of clipping the ctrl-y hint off-screen."""
    app = FooterApp()
    async with app.run_test(size=(100, 24)) as pilot:
        bar = app.query_one("#footer", FooterBar)
        bar.update_state(
            FooterState(
                mode_id="build",
                bundle="dev-bundle",
                session_short="a1b2c3",
                cost=Decimal("0.87"),
                waiting=1,
                context="idle",
            )
        )
        await pilot.pause()
        assert bar.has_class("-wrapped")
        assert bar.has_class("-badge-wrapped")
        badge = bar._badge
        assert badge.region.right <= 100
        assert badge.region.width >= len(footer_waiting_text(bar.state))
        await pilot.click(badge)
        await pilot.pause()
        assert len(app.messages) == 1


@pytest.mark.asyncio
async def test_footer_badge_stays_inline_at_wide_width() -> None:
    app = FooterApp()
    async with app.run_test(size=(160, 24)) as pilot:
        bar = app.query_one("#footer", FooterBar)
        bar.update_state(FooterState(waiting=1))
        await pilot.pause()
        assert not bar.has_class("-badge-wrapped")
        assert bar._badge.region.y == bar._left.region.y


@pytest.mark.asyncio
async def test_footer_hint_changes_with_context() -> None:
    app = FooterApp()
    async with app.run_test() as pilot:
        bar = app.query_one("#footer", FooterBar)
        bar.update_state(FooterState(context="running"))
        await pilot.pause()
        assert _plain(app.query_one("#footer-right", Static)) == (
            "esc interrupt · enter steer · shift+enter queue"
        )
        bar.update_state(FooterState(context="approval"))
        await pilot.pause()
        assert _plain(app.query_one("#footer-right", Static)) == (
            "arrows select · enter confirm · esc deny"
        )
