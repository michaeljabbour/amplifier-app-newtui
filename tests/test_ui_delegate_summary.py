"""DelegateSummaryBlock interaction: toggle via widget, archive, and scrollback."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from amplifier_app_newtui.model.blocks import (
    DelegateEntry,
    DelegateSummaryBlock,
    Narration,
    TranscriptBlock,
)
from amplifier_app_newtui.ui.themes import DEFAULT_THEME, register_themes, theme_id
from amplifier_app_newtui.ui.transcript import (
    HISTORY_COMPACT_TRIGGER,
    BlockWidget,
    DelegateSummaryToggled,
    HistoryArchive,
    TranscriptView,
)


class Harness(App[None]):
    """Minimal host app capturing delegate-summary toggle messages."""

    def __init__(self) -> None:
        super().__init__()
        register_themes(self)
        self.summaries: list[DelegateSummaryToggled] = []

    def on_mount(self) -> None:
        self.theme = theme_id(DEFAULT_THEME)

    def compose(self) -> ComposeResult:
        yield TranscriptView(id="transcript")

    def on_delegate_summary_toggled(self, message: DelegateSummaryToggled) -> None:
        self.summaries.append(message)


def _view(app: Harness) -> TranscriptView:
    return app.query_one("#transcript", TranscriptView)


def _mounted(view: TranscriptView, block: TranscriptBlock) -> BlockWidget:
    """Append a block and return its mounted flat BlockWidget."""
    widget = view.append(block)
    assert isinstance(widget, BlockWidget)
    return widget


SUMMARY = DelegateSummaryBlock(
    id="ds1",
    entries=(
        DelegateEntry(agent="researcher", state="done", elapsed_s=4.0, snippet="3 findings"),
        DelegateEntry(agent="tester", state="done", elapsed_s=2.0, snippet="tests ✔"),
    ),
    duration_s=6.0,
)


@pytest.mark.asyncio
async def test_activate_toggles_and_posts_message() -> None:
    app = Harness()
    async with app.run_test() as pilot:
        view = _view(app)
        widget = _mounted(view, SUMMARY)
        widget._activate()
        await pilot.pause()
        assert app.summaries and app.summaries[-1].expanded is True
        stored = view.get_block("ds1")
        assert isinstance(stored, DelegateSummaryBlock)
        assert stored.expanded is True  # canonical sync
        widget._activate()
        await pilot.pause()
        assert app.summaries[-1].expanded is False
        stored = view.get_block("ds1")
        assert isinstance(stored, DelegateSummaryBlock)
        assert stored.expanded is False


@pytest.mark.asyncio
async def test_summary_widget_takes_keyboard_focus() -> None:
    app = Harness()
    async with app.run_test():
        widget = _mounted(_view(app), SUMMARY)
        assert widget.can_focus is True


@pytest.mark.asyncio
async def test_archived_summary_still_toggles() -> None:
    """Compaction must not strip the toggle: every past summary in
    scrollback stays expandable (ambient-progress D5 durability)."""

    app = Harness()
    async with app.run_test(size=(100, 30)) as pilot:
        view = _view(app)
        view.append(SUMMARY)
        for index in range(HISTORY_COMPACT_TRIGGER + 20):
            view.append(Narration(id=f"archive-{index}", text=f"history line {index}"))
        await pilot.pause(0.2)

        archive = view.query_one(HistoryArchive)
        assert view.get_widget("ds1") is None  # consolidated, not mounted
        assert view.get_block("ds1") == SUMMARY

        archive.action_archive_activate("ds1")
        await pilot.pause()
        toggled = view.get_block("ds1")
        assert isinstance(toggled, DelegateSummaryBlock)
        assert toggled.expanded is True
        assert app.summaries and app.summaries[-1].block_id == "ds1"
        assert app.summaries[-1].expanded is True
        # The expanded agent rows are painted into the archive itself.
        assert "researcher" in str(archive.content)

        archive.action_archive_activate("ds1")
        await pilot.pause()
        collapsed = view.get_block("ds1")
        assert isinstance(collapsed, DelegateSummaryBlock)
        assert collapsed.expanded is False
