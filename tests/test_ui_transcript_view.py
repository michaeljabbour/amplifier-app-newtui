"""TranscriptView / BlockWidget behavior tests (Textual Pilot).

Covers DESIGN-SPEC §3 interactivity and ADR-0007 rendering decisions:
keyed append/replace/remove, in-place tool expand (click AND enter),
click messages (answer → ShowEvidence, turn rule → OpenRewind), lane-focus
swap + restore, tail-follow anchoring, and the 75ms debounced resize
reflow with streaming deferral.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from amplifier_app_newtui.model.blocks import (
    Answer,
    Narration,
    Segment,
    SessionBanner,
    ToolLine,
    TurnRule,
    UserLine,
)
from amplifier_app_newtui.model.evidence import EvidenceLink
from amplifier_app_newtui.ui.themes import DEFAULT_THEME, register_themes, theme_id
from amplifier_app_newtui.ui.transcript import (
    LaneFocusChanged,
    OpenRewind,
    ShowEvidence,
    ToolLineToggled,
    TranscriptView,
)


class Harness(App[None]):
    """Minimal host app capturing every transcript message."""

    def __init__(self) -> None:
        super().__init__()
        register_themes(self)
        self.evidence: list[ShowEvidence] = []
        self.rewinds: list[OpenRewind] = []
        self.toggles: list[ToolLineToggled] = []
        self.lane_changes: list[LaneFocusChanged] = []

    def on_mount(self) -> None:
        self.theme = theme_id(DEFAULT_THEME)

    def compose(self) -> ComposeResult:
        yield TranscriptView(id="transcript")

    def on_show_evidence(self, message: ShowEvidence) -> None:
        self.evidence.append(message)

    def on_open_rewind(self, message: OpenRewind) -> None:
        self.rewinds.append(message)

    def on_tool_line_toggled(self, message: ToolLineToggled) -> None:
        self.toggles.append(message)

    def on_lane_focus_changed(self, message: LaneFocusChanged) -> None:
        self.lane_changes.append(message)


def _view(app: Harness) -> TranscriptView:
    return app.query_one("#transcript", TranscriptView)


TOOL = ToolLine(
    id="b2",
    summary="Ran 2 shell commands",
    body=("1214 passed", "build succeeded"),
    status="completed",
)


@pytest.mark.asyncio
async def test_append_replace_remove_keyed_by_block_id() -> None:
    app = Harness()
    async with app.run_test(size=(80, 24)) as pilot:
        view = _view(app)
        view.append(UserLine(id="b1", text="hello", mode="chat"))
        view.append(Narration(id="b2", text="working on it"))
        await pilot.pause()
        assert view.block_ids == ("b1", "b2")

        view.replace(Narration(id="b2", text="revised narration"))
        await pilot.pause()
        assert view.get_block("b2").text == "revised narration"
        assert view.block_ids == ("b1", "b2")  # replace is in place

        view.remove_block("b1")
        await pilot.pause()
        assert view.block_ids == ("b2",)
        with pytest.raises(KeyError):
            view.replace(Narration(id="b1", text="gone"))
        with pytest.raises(ValueError):
            view.append(Narration(id="b2", text="duplicate"))


@pytest.mark.asyncio
async def test_tool_line_click_toggles_body_in_place() -> None:
    app = Harness()
    async with app.run_test(size=(80, 24)) as pilot:
        view = _view(app)
        widget = view.append(TOOL)
        await pilot.pause()
        assert view.get_block("b2").expanded is False

        await pilot.click(widget)
        assert view.get_block("b2").expanded is True
        assert app.toggles[-1].block_id == "b2" and app.toggles[-1].expanded is True
        # Same widget, same block id — toggled IN PLACE.
        assert view.block_ids == ("b2",)

        await pilot.click(widget)
        assert view.get_block("b2").expanded is False
        assert app.toggles[-1].expanded is False


@pytest.mark.asyncio
async def test_tool_line_enter_toggles_when_focused() -> None:
    app = Harness()
    async with app.run_test(size=(80, 24)) as pilot:
        view = _view(app)
        widget = view.append(TOOL)
        await pilot.pause()
        widget.focus()
        await pilot.press("enter")
        assert view.get_block("b2").expanded is True
        assert app.toggles[-1].expanded is True


@pytest.mark.asyncio
async def test_answer_click_posts_show_evidence() -> None:
    app = Harness()
    async with app.run_test(size=(80, 24)) as pilot:
        view = _view(app)
        links = (EvidenceLink(claim_quote="tests pass", tool_ref="pytest run"),)
        widget = view.append(
            Answer(id="b3", spans=(Segment(text="All done."),), evidence_refs=links)
        )
        await pilot.pause()
        await pilot.click(widget)
        assert len(app.evidence) == 1
        assert app.evidence[0].block_id == "b3"
        assert app.evidence[0].links == links


@pytest.mark.asyncio
async def test_turn_rule_click_posts_open_rewind_with_checkpoint_id() -> None:
    app = Harness()
    async with app.run_test(size=(80, 24)) as pilot:
        view = _view(app)
        widget = view.append(
            TurnRule(id="b4", checkpoint_id="t7", label="12s · 3.1k tok · $0.08 · answer")
        )
        await pilot.pause()
        await pilot.click(widget)
        assert [message.checkpoint_id for message in app.rewinds] == ["t7"]


@pytest.mark.asyncio
async def test_lane_focus_swaps_block_list_and_restore_brings_main_back() -> None:
    app = Harness()
    async with app.run_test(size=(80, 24)) as pilot:
        view = _view(app)
        view.append(UserLine(id="b1", text="parent turn", mode="build"))
        view.append(Narration(id="b2", text="spawning agents"))
        await pilot.pause()

        child_blocks = [
            SessionBanner(
                id="c1",
                headline="",
                focus_note=(
                    "focused: test-writer · subagent of a1b2c3 · own context window"
                    " · results report back to parent · esc back"
                ),
            ),
            UserLine(id="c2", text="write the tests", mode="delegated"),
        ]
        await view.focus_lane("lane-1", child_blocks)
        await pilot.pause()
        assert view.focused_lane == "lane-1"
        assert view.block_ids == ("c1", "c2")
        assert app.lane_changes[-1].lane_id == "lane-1"

        # Updates while focused address the visible (subagent) list.
        view.replace(UserLine(id="c2", text="write MORE tests", mode="delegated"))
        assert view.get_block("c2").text == "write MORE tests"

        await view.restore_main()  # the app's esc handler
        await pilot.pause()
        assert view.focused_lane is None
        assert view.block_ids == ("b1", "b2")
        assert view.get_block("b1").text == "parent turn"
        assert app.lane_changes[-1].lane_id is None


@pytest.mark.asyncio
async def test_tail_follow_sticks_to_bottom_until_user_scrolls_up() -> None:
    app = Harness()
    async with app.run_test(size=(60, 10)) as pilot:
        view = _view(app)
        for index in range(30):
            view.append(Narration(id=f"b{index}", text=f"line {index}"))
        await pilot.pause()
        await pilot.pause()
        assert view.follow is True
        assert view.is_vertical_scroll_end

        # User scrolls up: follow disengages, appends stop moving the view.
        view.scroll_to(y=0, animate=False)
        view.on_mouse_scroll_up(None)  # the user-scroll signal
        await pilot.pause()
        assert view.follow is False
        view.append(Narration(id="b99", text="new line while scrolled up"))
        await pilot.pause()
        await pilot.pause()
        assert not view.is_vertical_scroll_end

        # Scrolling back to the bottom re-arms following.
        view.scroll_end(animate=False)
        view.on_mouse_scroll_down(None)
        await pilot.pause()
        assert view.follow is True


@pytest.mark.asyncio
async def test_resize_reflow_debounced_and_width_pure() -> None:
    app = Harness()
    async with app.run_test(size=(100, 24)) as pilot:
        view = _view(app)
        widget = view.append(
            TurnRule(id="b1", checkpoint_id="t1", label="1s · 10 tok · $0.01 · answer")
        )
        await pilot.pause()
        first_width = widget._painted_width
        assert first_width == widget.size.width

        await pilot.resize_terminal(60, 24)
        await pilot.pause(0.25)  # > 75ms trailing debounce
        assert widget._painted_width == widget.size.width
        assert widget._painted_width != first_width


@pytest.mark.asyncio
async def test_resize_reflow_deferred_while_streaming_then_forced_once() -> None:
    app = Harness()
    async with app.run_test(size=(100, 24)) as pilot:
        view = _view(app)
        widget = view.append(
            TurnRule(id="b1", checkpoint_id="t1", label="1s · 10 tok · $0.01 · answer")
        )
        await pilot.pause()
        streamed_width = widget._painted_width

        view.set_streaming(True)
        await pilot.resize_terminal(60, 24)
        await pilot.pause(0.25)
        # Deferred: painted width is stale while the stream is active.
        assert widget._painted_width == streamed_width
        assert widget.size.width != streamed_width

        view.set_streaming(False)  # consolidation → exactly one forced reflow
        await pilot.pause()
        assert widget._painted_width == widget.size.width


@pytest.mark.asyncio
async def test_working_status_widget_pulses_spinner() -> None:
    from decimal import Decimal  # noqa: F401

    from amplifier_app_newtui.model.blocks import WorkingStatus
    from amplifier_app_newtui.model.turn import TurnTelemetry

    app = Harness()
    async with app.run_test(size=(80, 24)) as pilot:
        view = _view(app)
        widget = view.append(
            WorkingStatus(
                id="b1", telemetry=TurnTelemetry(secs=1, tokens_down=100), agent_count=0
            )
        )
        await pilot.pause(0.6)  # > one 260ms spinner interval
        assert widget._spinner_offset >= 1
        # Removing the block (turn end) also stops the pulse timer.
        view.remove_block("b1")
        await pilot.pause()
        assert widget._spin_timer is None
