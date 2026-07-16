"""Tests for ui/needs_you.py — needs-you block + focused-lane banner (spec §7/§8)."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from amplifier_app_newtui.model.blocks import (
    NeedsYouBlock,
    NeedsYouChoice,
    NeedsYouEntry,
)
from amplifier_app_newtui.ui.needs_you import (
    NeedsYouList,
    applying_decision_line,
    chip_text,
    decision_number_text,
    focused_lane_banner,
    focused_lane_banner_parts,
    needs_you_header,
    needs_you_header_line,
)
from amplifier_app_newtui.ui.themes import DEFAULT_THEME, register_themes, theme_id

# The mockup's deferred decision, verbatim.
BLOCK = NeedsYouBlock(
    id="b9",
    items=(
        NeedsYouEntry(
            decision_id="decision-1",
            question=(
                "Push branch to origin was blocked (outside trust boundary)."
                " Push to fork mj/waypoint instead?"
            ),
            choices=(
                NeedsYouChoice(label="yes · push to fork", answer="push to fork mj/waypoint"),
            ),
        ),
    ),
)


class NeedsYouHost(App[None]):
    def __init__(self) -> None:
        super().__init__()
        register_themes(self)
        self.theme = theme_id(DEFAULT_THEME)
        self.decisions: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        yield NeedsYouList(BLOCK)

    def on_needs_you_list_decision_taken(self, message: NeedsYouList.DecisionTaken) -> None:
        self.decisions.append((message.item_id, message.choice))


# -- pure helpers -------------------------------------------------------


def test_header_exact_strings() -> None:
    assert needs_you_header(1) == "Needs you  1 deferred decision"
    assert needs_you_header_line(2) == "· Needs you  2 deferred decision"


def test_chip_and_number_text() -> None:
    assert chip_text(BLOCK.items[0].choices[0]) == "[yes · push to fork]"
    assert decision_number_text(1) == "  1 "


def test_applying_decision_line() -> None:
    assert (
        applying_decision_line("pushing to fork mj/waypoint")
        == "Applying decision: pushing to fork mj/waypoint"
    )


def test_focused_lane_banner_exact_string() -> None:
    assert focused_lane_banner("researcher", "e07de0") == (
        "focused: researcher · subagent of e07de0 · own context window"
        " · results report back to parent · esc back"
    )
    prefix, tail = focused_lane_banner_parts("coder", "e07de0")
    assert prefix == "focused: coder "
    assert tail == (
        "· subagent of e07de0 · own context window"
        " · results report back to parent · esc back"
    )


# -- widget behavior ----------------------------------------------------


@pytest.mark.asyncio
async def test_block_renders_header_and_numbered_rows() -> None:
    app = NeedsYouHost()
    async with app.run_test() as pilot:
        widget = app.query_one(NeedsYouList)
        await pilot.pause()
        assert widget.header_text == "· Needs you  1 deferred decision"
        from amplifier_app_newtui.ui.needs_you import (  # test-only
            _ChoiceChip,
            _DecisionRow,
            _NeedsYouHeader,
        )

        assert widget.query_one(_NeedsYouHeader).count == 1
        rows = list(widget.query(_DecisionRow))
        assert len(rows) == 1
        assert rows[0].number == 1
        chips = list(widget.query(_ChoiceChip))
        assert len(chips) == 1
        assert str(chips[0].render()) == "[yes · push to fork]"


@pytest.mark.asyncio
async def test_chip_click_posts_decision_taken() -> None:
    app = NeedsYouHost()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.click("#chip-decision-1-0")
        await pilot.pause()
        assert app.decisions == [("decision-1", "push to fork mj/waypoint")]


@pytest.mark.asyncio
async def test_take_decision_programmatic_path() -> None:
    app = NeedsYouHost()
    async with app.run_test() as pilot:
        widget = app.query_one(NeedsYouList)
        await pilot.pause()
        widget.take_decision("decision-1", "push to fork mj/waypoint")
        await pilot.pause()
        assert app.decisions == [("decision-1", "push to fork mj/waypoint")]


@pytest.mark.asyncio
async def test_update_block_rerenders() -> None:
    app = NeedsYouHost()
    async with app.run_test() as pilot:
        widget = app.query_one(NeedsYouList)
        await pilot.pause()
        two = NeedsYouBlock(
            id="b10",
            items=(
                BLOCK.items[0],
                NeedsYouEntry(
                    decision_id="decision-2",
                    question="Install dependency left unresolved. Retry with lockfile?",
                    choices=(NeedsYouChoice(label="yes · retry", answer="retry with lockfile"),),
                ),
            ),
        )
        widget.update_block(two)
        await pilot.pause()
        assert widget.header_text == "· Needs you  2 deferred decision"
        from amplifier_app_newtui.ui.needs_you import _DecisionRow  # test-only

        assert [row.number for row in widget.query(_DecisionRow)] == [1, 2]
