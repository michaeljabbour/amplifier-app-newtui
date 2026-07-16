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

    def compose(self) -> ComposeResult:
        yield ApprovalBar(TICKET, PROMPT, id="approval")

    def on_mount(self) -> None:
        self.query_one("#approval", ApprovalBar).focus()

    def on_approval_bar_resolved(self, message: ApprovalBar.Resolved) -> None:
        self.resolved.append(message)


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
