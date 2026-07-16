"""LiveTail streaming-region tests (ADR-0007 two-region model).

Delta accumulation, 30Hz paint throttling, table holdback per
RESEARCH-BRIEF risk 1, and consolidation into a durable Answer block on
stream end.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from amplifier_app_newtui.model.blocks import Answer, Segment
from amplifier_app_newtui.model.evidence import EvidenceLink
from amplifier_app_newtui.ui.live_tail import (
    THROTTLE_SECONDS,
    LiveTail,
    answer_spans,
    visible_length,
)
from amplifier_app_newtui.ui.themes import DEFAULT_THEME, register_themes, theme_id


class TailHarness(App[None]):
    def __init__(self) -> None:
        super().__init__()
        register_themes(self)
        self.consolidated: list[Answer] = []

    def on_mount(self) -> None:
        self.theme = theme_id(DEFAULT_THEME)

    def compose(self) -> ComposeResult:
        yield LiveTail(id="tail")

    def on_live_tail_consolidated(self, message: LiveTail.Consolidated) -> None:
        self.consolidated.append(message.answer)


def _tail(app: TailHarness) -> LiveTail:
    return app.query_one("#tail", LiveTail)


# -- pure helpers --------------------------------------------------------------


def test_answer_spans_selective_emphasis() -> None:
    spans = answer_spans("Run `pytest` now — **done**.")
    assert spans == (
        Segment(text="Run "),
        Segment(text="pytest", style_token="teal"),
        Segment(text=" now — "),
        Segment(text="done", style_token="bright", bold=True),
        Segment(text="."),
    )


def test_answer_spans_plain_and_empty() -> None:
    assert answer_spans("just text") == (Segment(text="just text"),)
    assert answer_spans("") == (Segment(text=""),)


def test_visible_length_holds_back_trailing_table() -> None:
    # Trailing table run (with streaming-newline artifact) is withheld.
    assert visible_length(["Results:", "| a | b |", "| 1 | 2 |"]) == 1
    assert visible_length(["Results:", "| a | b |", ""]) == 1
    # No table → everything paints.
    assert visible_length(["Results:", "done"]) == 2
    # A paragraph break after the table completes it → paintable.
    assert visible_length(["Results:", "| a | b |", "", "Done"]) == 4


# -- widget behavior -----------------------------------------------------------


@pytest.mark.asyncio
async def test_feed_accumulates_and_visible_source_tracks() -> None:
    app = TailHarness()
    async with app.run_test(size=(80, 24)) as pilot:
        tail = _tail(app)
        tail.open_stream()
        tail.feed("Hello ")
        tail.feed("world")
        await pilot.pause(0.1)
        assert tail.source == "Hello world"
        assert tail.visible_source() == "Hello world"


@pytest.mark.asyncio
async def test_paints_throttle_to_one_per_interval() -> None:
    app = TailHarness()
    async with app.run_test(size=(80, 24)) as pilot:
        tail = _tail(app)
        tail.open_stream()
        base = tail.paint_count  # open_stream paints once
        for index in range(50):  # a burst far faster than 30Hz
            tail.feed(f"chunk{index} ")
        # The burst may cost at most one immediate paint + one trailing timer.
        assert tail.paint_count <= base + 1
        await pilot.pause(THROTTLE_SECONDS * 4)
        assert tail.paint_count <= base + 2
        assert tail.source.endswith("chunk49 ")
        # The trailing paint flushed the full accumulated source.
        assert tail.visible_source() == tail.source


@pytest.mark.asyncio
async def test_trailing_table_withheld_until_stream_end() -> None:
    app = TailHarness()
    async with app.run_test(size=(80, 24)) as pilot:
        tail = _tail(app)
        tail.open_stream()
        tail.feed("Results:\n| Check | State |\n| tests | pass |")
        await pilot.pause(0.1)
        assert tail.visible_source() == "Results:"  # table held back

        answer = tail.consolidate("b9")
        # Consolidation carries the FULL source, holdback never loses text.
        assert "".join(span.text for span in answer.spans) == (
            "Results:\n| Check | State |\n| tests | pass |"
        )


@pytest.mark.asyncio
async def test_consolidate_emits_answer_block_and_message_then_resets() -> None:
    app = TailHarness()
    async with app.run_test(size=(80, 24)) as pilot:
        tail = _tail(app)
        tail.open_stream()
        tail.feed("Run `pytest` — **34 passed**.\n")
        await pilot.pause(0.1)

        answer = tail.consolidate("b42")
        await pilot.pause()
        assert answer.id == "b42"
        assert answer.spans == (
            Segment(text="Run "),
            Segment(text="pytest", style_token="teal"),
            Segment(text=" — "),
            Segment(text="34 passed", style_token="bright", bold=True),
            Segment(text="."),
        )
        assert app.consolidated == [answer]  # message-based wiring
        assert tail.source == ""  # tail cleared for the next stream

        with_refs = tail.attach_evidence(
            answer, (EvidenceLink(claim_quote="34 passed", tool_ref="pytest run"),)
        )
        assert with_refs.evidence_refs[0].tool_ref == "pytest run"
        assert with_refs.id == "b42"


@pytest.mark.asyncio
async def test_thinking_blocks_paint_italic_dim() -> None:
    app = TailHarness()
    async with app.run_test(size=(80, 24)) as pilot:
        tail = _tail(app)
        tail.open_stream(block_type="thinking")
        tail.feed("considering the store layout")
        await pilot.pause(0.1)
        assert tail.block_type == "thinking"
        assert tail._markup().startswith("[italic $dim]")


@pytest.mark.asyncio
async def test_open_stream_resets_previous_source() -> None:
    app = TailHarness()
    async with app.run_test(size=(80, 24)) as pilot:
        tail = _tail(app)
        tail.open_stream()
        tail.feed("first stream")
        await pilot.pause(0.1)
        tail.open_stream()
        assert tail.source == ""
        assert tail.visible_source() == ""
