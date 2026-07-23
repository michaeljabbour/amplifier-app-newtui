"""Tests for the approval bar (ui/approval_bar.py) — DESIGN-SPEC §7."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from amplifier_app_newtui.ui.approval_bar import (
    APPROVAL_LABEL,
    DEFAULT_OPTIONS,
    ApprovalBar,
    ApprovalOption,
)
from amplifier_app_newtui.ui.themes import DEFAULT_THEME, register_themes, theme_id

TICKET = "ticket-42"
PROMPT = "Run `pytest -q` in /repo?"


class ApprovalApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        register_themes(self)
        self.theme = theme_id(DEFAULT_THEME)
        self.resolved: list[ApprovalBar.Resolved] = []
        self.deferred: list[ApprovalBar.Deferred] = []

    def compose(self) -> ComposeResult:
        yield ApprovalBar(TICKET, PROMPT, id="approval")

    def on_mount(self) -> None:
        self.query_one("#approval", ApprovalBar).focus()

    def on_approval_bar_resolved(self, message: ApprovalBar.Resolved) -> None:
        self.resolved.append(message)

    def on_approval_bar_deferred(self, message: ApprovalBar.Deferred) -> None:
        self.deferred.append(message)


def test_default_options_are_verbatim_fail_closed_strings() -> None:
    assert DEFAULT_OPTIONS == ("Allow once", "Allow always", "Deny")


def test_label_is_exact_spec_string() -> None:
    assert APPROVAL_LABEL == "Approval required ·"


def test_option_texts_selected_prefix() -> None:
    bar = ApprovalBar(TICKET, PROMPT)
    assert bar.option_texts() == ("› Allow once", "Allow always", "Deny")


@pytest.mark.asyncio
async def test_rendered_strings_and_selection_styling() -> None:
    app = ApprovalApp()
    async with app.run_test() as pilot:
        bar = app.query_one("#approval", ApprovalBar)
        labels = [str(w.render()) for w in app.query(Static) if not isinstance(w, ApprovalOption)]
        assert any(APPROVAL_LABEL in text for text in labels)
        assert any(PROMPT in text for text in labels)

        options = list(app.query(ApprovalOption))
        assert [str(o.content) for o in options] == [
            "› Allow once",
            "Allow always",
            "Deny",
        ]
        # Selected bright-on-bg-tab; Deny red while unselected.
        assert options[0].has_class("-selected")
        assert options[2].has_class("-deny")

        await pilot.press("right")
        assert bar.selected == 1
        assert [str(o.content) for o in options] == [
            "Allow once",
            "› Allow always",
            "Deny",
        ]


@pytest.mark.asyncio
async def test_arrows_and_tab_cycle_with_wraparound() -> None:
    app = ApprovalApp()
    async with app.run_test() as pilot:
        bar = app.query_one("#approval", ApprovalBar)
        await pilot.press("left")  # wraps 0 -> 2
        assert bar.selected == 2
        await pilot.press("tab")  # wraps 2 -> 0
        assert bar.selected == 0
        await pilot.press("down")
        assert bar.selected == 1
        await pilot.press("up")
        assert bar.selected == 0


@pytest.mark.asyncio
async def test_enter_confirms_selected_option() -> None:
    app = ApprovalApp()
    async with app.run_test() as pilot:
        await pilot.press("right", "enter")
        await pilot.pause()
        assert len(app.resolved) == 1
        assert app.resolved[0].ticket_id == TICKET
        assert app.resolved[0].choice == "Allow always"


@pytest.mark.asyncio
async def test_escape_resolves_to_deny() -> None:
    app = ApprovalApp()
    async with app.run_test() as pilot:
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.resolved) == 1
        assert app.resolved[0].choice == "Deny"


@pytest.mark.asyncio
async def test_click_confirms_that_option() -> None:
    app = ApprovalApp()
    async with app.run_test() as pilot:
        await pilot.pause()  # let the initial resize settle the wrap layout
        options = list(app.query(ApprovalOption))
        await pilot.click(options[2])
        await pilot.pause()
        assert len(app.resolved) == 1
        assert app.resolved[0].choice == "Deny"


@pytest.mark.asyncio
async def test_selecting_deny_swaps_red_for_selected_styling() -> None:
    app = ApprovalApp()
    async with app.run_test() as pilot:
        options = list(app.query(ApprovalOption))
        assert options[2].has_class("-deny")
        await pilot.press("left")  # select Deny
        assert options[2].has_class("-selected")
        assert not options[2].has_class("-deny")
        assert str(options[2].content) == "› Deny"


def test_empty_options_rejected() -> None:
    with pytest.raises(ValueError):
        ApprovalBar(TICKET, PROMPT, options=())


@pytest.mark.asyncio
async def test_selected_option_is_bold() -> None:
    """Mockup approvalOptions: selected renders font-weight 700."""
    app = ApprovalApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        options = list(app.query(ApprovalOption))
        assert options[0].styles.text_style.bold
        assert not options[1].styles.text_style.bold


@pytest.mark.asyncio
async def test_options_wrap_onto_second_row_at_narrow_width() -> None:
    """Mockup approval strip has flex-wrap: wrap — at 80 cols every option
    stays on-screen (visible and clickable) instead of clipping (spec §7)."""
    app = ApprovalApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        bar = app.query_one("#approval", ApprovalBar)
        assert bar.has_class("-wrapped")
        options = list(app.query(ApprovalOption))
        for option in options:
            assert option.region.right <= 80
        await pilot.click(options[2])
        await pilot.pause()
        assert app.resolved and app.resolved[0].choice == "Deny"


@pytest.mark.asyncio
async def test_options_stay_on_one_row_at_wide_width() -> None:
    app = ApprovalApp()
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        bar = app.query_one("#approval", ApprovalBar)
        assert not bar.has_class("-wrapped")
        assert bar.size.height == 1


@pytest.mark.asyncio
async def test_ctrl_y_parks_ticket_without_resolving() -> None:
    """ctrl-y posts Deferred(ticket_id) — the park path (ADR-0007
    approvals) — and must NOT resolve the ticket (no answer chosen)."""
    app = ApprovalApp()
    async with app.run_test() as pilot:
        await pilot.press("ctrl+y")
        await pilot.pause()
        assert len(app.deferred) == 1
        assert app.deferred[0].ticket_id == TICKET
        # A park is not a decision: no Resolved message is emitted.
        assert app.resolved == []


@pytest.mark.asyncio
async def test_ctrl_y_park_leaves_selection_untouched() -> None:
    """Parking does not move the selection or answer — the bar just
    hands the ticket to the needs-you queue."""
    app = ApprovalApp()
    async with app.run_test() as pilot:
        bar = app.query_one("#approval", ApprovalBar)
        await pilot.press("right")  # select "Allow always"
        assert bar.selected == 1
        await pilot.press("ctrl+y")
        await pilot.pause()
        assert bar.selected == 1
        assert app.deferred and app.resolved == []
