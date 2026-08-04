"""TranscriptView / BlockWidget behavior tests (Textual Pilot).

Covers DESIGN-SPEC §3 interactivity and ADR-0007 rendering decisions:
keyed append/replace/remove, in-place tool expand (click AND enter),
click messages (answer → ShowEvidence, turn rule → OpenRewind), lane-focus
swap + restore, tail-follow anchoring, and the 75ms debounced resize
reflow with streaming deferral.
"""

from __future__ import annotations

from typing import TypeVar, cast

import pytest
from textual import events
from textual.app import App, ComposeResult
from textual.selection import SELECT_ALL

from amplifier_app_tui.model.blocks import (
    Answer,
    DelegateEntry,
    DelegateSummaryBlock,
    EvidenceBlock,
    Narration,
    NeedsYouBlock,
    NeedsYouChoice,
    NeedsYouEntry,
    Segment,
    SessionBanner,
    ToolLine,
    TranscriptBlock,
    TurnRule,
    UserLine,
)
from amplifier_app_tui.model.evidence import EvidenceLink
from amplifier_app_tui.ui.live_tail import answer_spans
from amplifier_app_tui.ui.needs_you import NeedsYouList
from amplifier_app_tui.ui.themes import DEFAULT_THEME, register_themes, theme_id
from amplifier_app_tui.ui.transcript import (
    BackToParent,
    BlockWidget,
    CloseEvidence,
    CopyCodeFence,
    ExpandEvidenceClaim,
    FOCUS_HEADER_HINT,
    FOCUS_HEADER_TITLE,
    FocusHeader,
    HISTORY_COMPACT_TRIGGER,
    HISTORY_WIDGET_LIMIT,
    HistoryArchive,
    LaneFocusChanged,
    OpenRewind,
    ShowEvidence,
    ToolLineToggled,
    TranscriptView,
    fence_text_at_row,
    render_block,
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
        self.expanded_claims: list[ExpandEvidenceClaim] = []
        self.closed_evidence: list[CloseEvidence] = []
        self.decisions: list[NeedsYouList.DecisionTaken] = []
        self.fence_copies: list[CopyCodeFence] = []
        self.back_to_parent: list[BackToParent] = []

    def on_mount(self) -> None:
        self.theme = theme_id(DEFAULT_THEME)

    def on_copy_code_fence(self, message: CopyCodeFence) -> None:
        self.fence_copies.append(message)

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

    def on_expand_evidence_claim(self, message: ExpandEvidenceClaim) -> None:
        self.expanded_claims.append(message)

    def on_close_evidence(self, message: CloseEvidence) -> None:
        self.closed_evidence.append(message)

    def on_needs_you_list_decision_taken(self, message: NeedsYouList.DecisionTaken) -> None:
        self.decisions.append(message)

    def on_back_to_parent(self, message: BackToParent) -> None:
        self.back_to_parent.append(message)


def _view(app: Harness) -> TranscriptView:
    return app.query_one("#transcript", TranscriptView)


BlockT = TypeVar("BlockT")


def _block(view: TranscriptView, block_id: str, kind: type[BlockT]) -> BlockT:
    """The stored block, asserted to be the expected kind (type-narrowed)."""
    block = view.get_block(block_id)
    assert isinstance(block, kind)
    return block


def _mounted(view: TranscriptView, block: TranscriptBlock) -> BlockWidget:
    """Append a block and return its mounted flat BlockWidget."""
    widget = view.append(block)
    assert isinstance(widget, BlockWidget)
    return widget


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
        assert _block(view, "b2", Narration).text == "revised narration"
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
        widget = _mounted(view, TOOL)
        await pilot.pause()
        assert _block(view, "b2", ToolLine).expanded is False

        await pilot.click(widget)
        assert _block(view, "b2", ToolLine).expanded is True
        assert app.toggles[-1].block_id == "b2" and app.toggles[-1].expanded is True
        # Same widget, same block id — toggled IN PLACE.
        assert view.block_ids == ("b2",)

        await pilot.click(widget)
        assert _block(view, "b2", ToolLine).expanded is False
        assert app.toggles[-1].expanded is False


@pytest.mark.asyncio
async def test_tool_line_enter_toggles_when_focused() -> None:
    app = Harness()
    async with app.run_test(size=(80, 24)) as pilot:
        view = _view(app)
        widget = _mounted(view, TOOL)
        await pilot.pause()
        widget.focus()
        await pilot.press("enter")
        assert _block(view, "b2", ToolLine).expanded is True
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
async def test_clicking_a_code_fence_copies_just_that_fence() -> None:
    """A click on a fenced code row posts CopyCodeFence with the dedented
    fence source (finer-grained than /copy's whole-answer grab); a click
    anywhere else on the answer still opens evidence."""
    app = Harness()
    async with app.run_test(size=(80, 24)) as pilot:
        view = _view(app)
        src = "Intro line.\n\n```python\nprint('hi')\nx = 1\n```"
        links = (EvidenceLink(claim_quote="c", tool_ref="r"),)
        widget = view.append(Answer(id="b7", spans=answer_spans(src), evidence_refs=links))
        await pilot.pause()
        lines = render_block(widget.block, widget.size.width)
        fence_row = next(i for i in range(len(lines)) if fence_text_at_row(lines, i))
        await pilot.click(widget, offset=(3, fence_row))
        await pilot.pause()
        assert [message.text for message in app.fence_copies] == ["print('hi')\nx = 1"]
        assert app.evidence == []  # a fence click never opens evidence
        # A click on the intro (row 0) is not a fence — evidence still opens.
        await pilot.click(widget, offset=(2, 0))
        await pilot.pause()
        assert [message.block_id for message in app.evidence] == ["b7"]


@pytest.mark.asyncio
async def test_inert_answer_lines_ignore_clicks() -> None:
    """Mockup click: null lines (agent tree rows, ✳ recap-shaped lines)
    are NOT evidence click targets — clicking them posts nothing."""
    app = Harness()
    async with app.run_test(size=(80, 24)) as pilot:
        view = _view(app)
        tree = view.append(
            Answer(
                id="b5",
                spans=(
                    Segment(text="  ├─ ✔ ", style_token="green"),
                    Segment(text="researcher · done", style_token="dim"),
                ),
                clickable=False,
            )
        )
        recap = view.append(
            Answer(
                id="b6",
                spans=(
                    Segment(text="✳ ", style_token="dimmer"),
                    Segment(text="Plan ready.", style_token="dim", italic=True),
                ),
                clickable=False,
            )
        )
        await pilot.pause()
        await pilot.click(tree)
        await pilot.click(recap)
        assert app.evidence == []


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

        # Mutations while focused address the stashed PARENT list (spec §8:
        # the parent turn keeps accumulating during focus; the child view
        # is a read-only snapshot) — mockup this.lines vs focusLines.
        view.replace(Narration(id="b2", text="agents finishing up"))
        view.append(Narration(id="b3", text="final answer landed"))
        view.remove_block("b1")
        with pytest.raises(KeyError):
            view.replace(UserLine(id="c2", text="child ids are not addressable", mode="delegated"))
        assert view.block_ids == ("c1", "c2")  # visible child list untouched

        await view.restore_main()  # the app's esc handler
        await pilot.pause()
        assert view.focused_lane is None
        assert view.block_ids == ("b2", "b3")
        assert _block(view, "b2", Narration).text == "agents finishing up"
        assert _block(view, "b3", Narration).text == "final answer landed"
        assert app.lane_changes[-1].lane_id is None


def _lane_blocks(prefix: str, count: int, focus_note: str) -> list[TranscriptBlock]:
    blocks: list[TranscriptBlock] = [
        SessionBanner(id=f"{prefix}-banner", headline="", focus_note=focus_note)
    ]
    blocks.extend(Narration(id=f"{prefix}{i}", text=f"{prefix} line {i}") for i in range(count))
    return blocks


_CHILD_BANNER = SessionBanner(
    id="c1",
    headline="",
    focus_note=(
        "focused: test-writer \u00b7 subagent of a1b2c3 \u00b7 own context window"
        " \u00b7 results report back to parent \u00b7 esc back"
    ),
)


@pytest.mark.asyncio
async def test_focus_header_back_control_click_and_keyboard() -> None:
    """S6 AC1/AC5: a visible 'Back to parent' control mounts only while a
    lane is focused, and both click (mouse) and enter/space (keyboard)
    post the same BackToParent message the app routes through
    restore_main() \u2014 the same seam Escape uses."""
    app = Harness()
    async with app.run_test(size=(80, 24)) as pilot:
        view = _view(app)
        view.append(UserLine(id="b1", text="parent turn", mode="build"))
        await pilot.pause()
        assert list(view.query(FocusHeader)) == []  # not focused yet: no Back control

        await view.focus_lane(
            "lane-1", [_CHILD_BANNER, UserLine(id="c2", text="hi", mode="delegated")]
        )
        await pilot.pause()
        headers = list(view.query(FocusHeader))
        assert len(headers) == 1
        header = headers[0]
        rendered = header.render().plain
        assert "Back to parent" in rendered  # AC1: the visible action
        assert "esc" in rendered  # AC1: its keyboard shortcut, shown alongside it
        assert FOCUS_HEADER_TITLE.endswith("Back to parent")
        assert "esc" in FOCUS_HEADER_HINT

        # Mouse: a click posts BackToParent.
        await pilot.click(header)
        await pilot.pause()
        assert len(app.back_to_parent) == 1

        # Keyboard: enter AND space also activate it once it holds focus.
        header.focus()
        await pilot.press("enter")
        assert len(app.back_to_parent) == 2
        await pilot.press("space")
        assert len(app.back_to_parent) == 3

        # It never rides along with the parent view.
        await view.restore_main()
        await pilot.pause()
        assert list(view.query(FocusHeader)) == []


@pytest.mark.asyncio
async def test_scroll_anchor_restores_each_views_own_position() -> None:
    """S6 AC3: leaving a view remembers its scroll position; returning
    (or re-entering) restores THAT view's own position instead of always
    snapping to the tail. A view visited for the first time still
    defaults to bottom-follow (today's behavior, unchanged)."""
    app = Harness()
    async with app.run_test(size=(60, 8)) as pilot:
        view = _view(app)
        for index in range(30):
            view.append(Narration(id=f"m{index}", text=f"main line {index}"))
        await pilot.pause()
        assert view.follow is True

        # The user reads partway up the parent before ever focusing a lane.
        view.scroll_to(y=0, animate=False)
        view.on_mouse_scroll_up(cast(events.MouseScrollUp, None))
        await pilot.pause()
        assert view.follow is False
        main_scroll_y = view.scroll_y

        lane_a = _lane_blocks("a", 30, "focused: a")
        await view.focus_lane("lane-a", lane_a)
        await pilot.pause()
        assert view.follow is True  # lane-a is a first-ever visit: bottom-follow

        # The user scrolls up inside lane-a's own transcript too.
        view.scroll_to(y=1, animate=False)
        view.on_mouse_scroll_up(cast(events.MouseScrollUp, None))
        await pilot.pause()
        assert view.follow is False
        lane_a_scroll_y = view.scroll_y
        assert lane_a_scroll_y != main_scroll_y  # a meaningfully distinct anchor

        await view.restore_main()
        await pilot.pause()
        # The parent comes back exactly where it was left, not the tail.
        assert view.follow is False
        assert view.scroll_y == pytest.approx(main_scroll_y)

        # Re-focusing the SAME lane (repeated enter/exit cycles) restores
        # ITS OWN remembered position too, not main's and not the tail.
        await view.focus_lane("lane-a", lane_a)
        await pilot.pause()
        assert view.follow is False
        assert view.scroll_y == pytest.approx(lane_a_scroll_y)


@pytest.mark.asyncio
async def test_scroll_anchor_independent_across_a_direct_lane_to_lane_hop() -> None:
    """S6 AC3 (nested panel transitions): the lanes panel stays open
    while focused, so the user can jump straight from one lane to
    another without returning to main first. Each view's own anchor
    stays independent across that hop, and the parent's anchor \u2014
    captured once, before either lane was ever entered \u2014 is untouched
    by whatever happens between lanes."""
    app = Harness()
    async with app.run_test(size=(60, 8)) as pilot:
        view = _view(app)
        for index in range(30):
            view.append(Narration(id=f"m{index}", text=f"main line {index}"))
        await pilot.pause()

        view.scroll_to(y=0, animate=False)
        view.on_mouse_scroll_up(cast(events.MouseScrollUp, None))
        await pilot.pause()
        main_scroll_y = view.scroll_y

        lane_a = _lane_blocks("a", 30, "focused: a")
        lane_b = _lane_blocks("b", 30, "focused: b")
        await view.focus_lane("lane-a", lane_a)
        await pilot.pause()
        view.scroll_to(y=2, animate=False)
        view.on_mouse_scroll_up(cast(events.MouseScrollUp, None))
        await pilot.pause()
        lane_a_scroll_y = view.scroll_y

        # Jump straight to lane-b \u2014 no return to main in between (the
        # lanes panel permits this: it never touches lanesOpen on focus).
        await view.focus_lane("lane-b", lane_b)
        await pilot.pause()
        assert view.follow is True  # lane-b is a first-ever visit

        await view.restore_main()
        await pilot.pause()
        # Main is restored to where it was BEFORE ever entering lane-a \u2014
        # untouched by the a -> b hop in between.
        assert view.follow is False
        assert view.scroll_y == pytest.approx(main_scroll_y)

        # And lane-a's own remembered position survived the round trip.
        await view.focus_lane("lane-a", lane_a)
        await pilot.pause()
        assert view.follow is False
        assert view.scroll_y == pytest.approx(lane_a_scroll_y)


@pytest.mark.asyncio
async def test_expanded_delegate_summary_survives_a_focus_round_trip() -> None:
    """S6 AC3 ('relevant panel state'): a block the user expanded in the
    parent (a nested, click-to-expand panel) stays expanded across a
    focus/return cycle \u2014 the stashed parent list is the live block
    state, not a stale copy frozen at focus time."""
    app = Harness()
    async with app.run_test(size=(80, 24)) as pilot:
        view = _view(app)
        summary = DelegateSummaryBlock(
            id="s1",
            entries=(DelegateEntry(agent="researcher", state="done", snippet="ok"),),
            duration_s=1.0,
        )
        widget = view.append(summary)
        await pilot.pause()
        assert isinstance(widget, BlockWidget)
        await pilot.click(widget)  # expand it
        await pilot.pause()
        assert _block(view, "s1", DelegateSummaryBlock).expanded is True

        await view.focus_lane("lane-1", [_CHILD_BANNER])
        await pilot.pause()
        await view.restore_main()
        await pilot.pause()
        assert _block(view, "s1", DelegateSummaryBlock).expanded is True


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
        # The handlers never read the event — a cast None stands in.
        view.on_mouse_scroll_up(cast(events.MouseScrollUp, None))
        await pilot.pause()
        assert view.follow is False
        view.append(Narration(id="b99", text="new line while scrolled up"))
        await pilot.pause()
        await pilot.pause()
        assert not view.is_vertical_scroll_end

        # Scrolling back to the bottom re-arms following.
        view.scroll_end(animate=False)
        view.on_mouse_scroll_down(cast(events.MouseScrollDown, None))
        await pilot.pause()
        assert view.follow is True


@pytest.mark.asyncio
async def test_resize_reflow_debounced_and_width_pure() -> None:
    app = Harness()
    async with app.run_test(size=(100, 24)) as pilot:
        view = _view(app)
        widget = _mounted(
            view,
            TurnRule(id="b1", checkpoint_id="t1", label="1s · 10 tok · $0.01 · answer"),
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
        widget = _mounted(
            view,
            TurnRule(id="b1", checkpoint_id="t1", label="1s · 10 tok · $0.01 · answer"),
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

    from amplifier_app_tui.model.blocks import WorkingStatus
    from amplifier_app_tui.model.turn import TurnTelemetry
    from amplifier_app_tui.ui.transcript import SPINNER_INTERVAL_SECONDS

    # Mockup runTurn: the working-line glyph advances once per second
    # inside the 1000ms tick (the 260ms spinTimer is the title bar's).
    assert SPINNER_INTERVAL_SECONDS == pytest.approx(1.0)

    app = Harness()
    async with app.run_test(size=(80, 24)) as pilot:
        view = _view(app)
        widget = _mounted(
            view,
            WorkingStatus(id="b1", telemetry=TurnTelemetry(secs=1, tokens_down=100), agent_count=0),
        )
        await pilot.pause(SPINNER_INTERVAL_SECONDS + 0.2)  # > one 1s glyph interval
        assert widget._spinner_offset >= 1
        # Removing the block (turn end) also stops the pulse timer.
        view.remove_block("b1")
        await pilot.pause()
        assert widget._spin_timer is None


@pytest.mark.asyncio
async def test_old_history_compacts_without_losing_text_or_actions() -> None:
    """The archive preserves infinite scroll/copy and old tool interactivity."""

    app = Harness()
    async with app.run_test(size=(100, 30)) as pilot:
        view = _view(app)
        old_tool = ToolLine(
            id="old-tool",
            summary="Read the original setup",
            body=("README.md", "config.yaml"),
            status="completed",
        )
        view.append(old_tool)
        for index in range(HISTORY_COMPACT_TRIGGER + 20):
            view.append(Narration(id=f"archive-{index}", text=f"history line {index}"))
        await pilot.pause(0.2)

        archive = view.query_one(HistoryArchive)
        assert len(view._widgets) <= HISTORY_WIDGET_LIMIT
        assert view.get_widget("old-tool") is None
        assert view.get_block("old-tool") == old_tool
        selected = archive.get_selection(SELECT_ALL)
        assert selected is not None
        assert "Read the original setup" in selected[0]
        assert "history line 0" in selected[0]

        view.release_anchor()
        view.scroll_to(y=0, animate=False)
        await pilot.pause()
        await pilot.click(archive, offset=(10, 0))
        await pilot.pause()
        expanded = _block(view, "old-tool", ToolLine)
        assert expanded.expanded is True
        assert "README.md" in str(archive.content)
        assert app.toggles[-1].block_id == "old-tool"

        view.remove_block("old-tool")
        await pilot.pause()
        assert view.get_block("old-tool") is None
        assert "Read the original setup" not in str(archive.content)


@pytest.mark.asyncio
async def test_archived_history_retains_answer_rewind_evidence_and_decisions() -> None:
    """Consolidation retains every non-tool interaction contract."""

    app = Harness()
    async with app.run_test(size=(100, 30)) as pilot:
        view = _view(app)
        link = EvidenceLink(claim_quote="the claim", tool_ref="read_file · source.py")
        view.append(
            Answer(
                id="old-answer",
                spans=(Segment(text="Grounded answer"),),
                evidence_refs=(link,),
            )
        )
        view.append(TurnRule(id="old-turn", checkpoint_id="checkpoint-7", label="7s · answer"))
        view.append(EvidenceBlock(id="old-evidence", links=(link,)))
        view.append(
            NeedsYouBlock(
                id="old-decision",
                items=(
                    NeedsYouEntry(
                        decision_id="decision-1",
                        question="Apply the safe change?",
                        choices=(NeedsYouChoice(label="yes", answer="apply it"),),
                    ),
                ),
            )
        )
        for index in range(HISTORY_COMPACT_TRIGGER + 20):
            view.append(Narration(id=f"tail-{index}", text=f"tail line {index}"))
        await pilot.pause(0.2)

        archive = view.query_one(HistoryArchive)
        archive.action_archive_activate("old-answer")
        archive.action_archive_activate("old-turn")
        archive.action_archive_activate("old-evidence")
        archive.action_evidence_expand()
        archive.action_close_evidence()
        archive.action_archive_decision("old-decision", 0, 0)
        await pilot.pause()

        assert app.evidence[-1].block_id == "old-answer"
        assert app.rewinds[-1].checkpoint_id == "checkpoint-7"
        assert app.expanded_claims[-1].link == link
        assert app.closed_evidence[-1].block_id == "old-evidence"
        assert app.decisions[-1].item_id == "decision-1"
        assert app.decisions[-1].choice == "apply it"


# ---------------------------------------------------------------------------
# clear_view() -- D3: /clear must visibly empty the transcript, not just
# clear the conversation context (Compliance 2026-08-02).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_view_removes_every_mounted_block() -> None:
    """AC1: clear_view() removes ALL rendered rows, mounted or not."""

    app = Harness()
    async with app.run_test(size=(80, 24)) as pilot:
        view = _view(app)
        view.append(UserLine(id="u1", text="hello", mode="chat"))
        view.append(Answer(id="a1", spans=(Segment(text="hi there"),)))
        view.append(ToolLine(id="t1", summary="ran a command", status="completed"))
        await pilot.pause()
        assert view.blocks  # sanity: something is actually there first

        view.clear_view()
        await pilot.pause()

        assert view.blocks == ()
        assert view.block_ids == ()
        assert view.get_block("u1") is None
        assert len(view.query(BlockWidget)) == 0


@pytest.mark.asyncio
async def test_clear_view_also_drops_the_history_archive() -> None:
    """AC1: consolidated (archived) older history is removed too, not just
    the recent widget-backed tail."""

    app = Harness()
    async with app.run_test(size=(100, 30)) as pilot:
        view = _view(app)
        for index in range(HISTORY_COMPACT_TRIGGER + 20):
            view.append(Narration(id=f"clear-archive-{index}", text=f"line {index}"))
        await pilot.pause(0.2)
        assert view.query(HistoryArchive)  # sanity: compaction actually ran

        view.clear_view()
        await pilot.pause()

        assert len(view.query(HistoryArchive)) == 0
        assert view.blocks == ()


@pytest.mark.asyncio
async def test_clear_view_resets_the_tail_follow_anchor() -> None:
    """AC2: a cleared view re-anchors to the bottom so it reads as an
    unmistakably empty, freshly-following page -- never stranded mid-scroll
    from wherever the user had scrolled before the clear."""

    app = Harness()
    async with app.run_test(size=(60, 10)) as pilot:
        view = _view(app)
        for index in range(40):
            view.append(Narration(id=f"n{index}", text=f"line {index}"))
        await pilot.pause()
        view.release_anchor()  # user scrolled up before clearing
        assert not view.follow

        view.clear_view()
        await pilot.pause()

        assert view.follow


@pytest.mark.asyncio
async def test_clear_view_is_idempotent_on_an_already_empty_view() -> None:
    """AC5 repeated clear: clearing an already-empty view is a safe no-op."""

    app = Harness()
    async with app.run_test(size=(80, 24)) as pilot:
        view = _view(app)
        view.clear_view()
        await pilot.pause()
        view.clear_view()  # a second /clear back-to-back
        await pilot.pause()
        assert view.blocks == ()


@pytest.mark.asyncio
async def test_clear_view_then_append_renders_fresh_content() -> None:
    """AC3: new output appears normally in the cleared view -- clearing
    never leaves the widget unable to mount anything afterward."""

    app = Harness()
    async with app.run_test(size=(80, 24)) as pilot:
        view = _view(app)
        view.append(Narration(id="pre-clear", text="before"))
        view.clear_view()
        await pilot.pause()

        widget = _mounted(view, Narration(id="post-clear", text="after the clear"))
        await pilot.pause()

        assert view.blocks == (Narration(id="post-clear", text="after the clear"),)
        assert widget.is_mounted
