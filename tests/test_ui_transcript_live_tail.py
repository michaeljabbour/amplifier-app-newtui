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
    ASYNC_RENDER_THRESHOLD,
    MAX_ROOT_LINES,
    THROTTLE_SECONDS,
    LiveTail,
    answer_spans,
    lane_tail_markup,
    streaming_spans,
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


def test_answer_spans_blockquote_callout_gutter() -> None:
    """``> ``-quoted lines render behind a colored left gutter — the
    TUI-native form of the insight/machete callouts the (formerly
    suppressed) hooks-inline-blocks module teaches the model to emit as
    Markdown blockquotes. Inline emphasis still applies inside the quote."""
    spans = answer_spans("> ★ **Insight:** one owner per concern.")
    assert spans == (
        Segment(text="▌ ", style_token="blue"),
        Segment(text="★ "),
        Segment(text="Insight:", style_token="bright", bold=True),
        Segment(text=" one owner per concern."),
    )


def test_answer_spans_blockquote_run_reads_as_its_own_paragraph() -> None:
    """A quote run gets one blank line before and after (like headings and
    lists); every quoted line carries the gutter; bare ``>`` still quotes."""
    spans = answer_spans("intro\n> ★ **Insight:** a\n>b\ntail")
    assert spans == (
        Segment(text="intro"),
        Segment(text="\n"),
        Segment(text="\n"),
        Segment(text="▌ ", style_token="blue"),
        Segment(text="★ "),
        Segment(text="Insight:", style_token="bright", bold=True),
        Segment(text=" a"),
        Segment(text="\n"),
        Segment(text="▌ ", style_token="blue"),
        Segment(text="b"),
        Segment(text="\n"),
        Segment(text="\n"),
        Segment(text="tail"),
    )


def test_answer_spans_italic_emphasis() -> None:
    """``*italic*`` maps to the italic flag alongside ``**bold**`` and
    `` `code` `` without colliding with either; a star adjacent to
    whitespace stays literal (arithmetic, globs)."""
    assert answer_spans("plain *emph* and **bold** and `code`") == (
        Segment(text="plain "),
        Segment(text="emph", italic=True),
        Segment(text=" and "),
        Segment(text="bold", style_token="bright", bold=True),
        Segment(text=" and "),
        Segment(text="code", style_token="teal"),
    )
    # A star with whitespace on the inside is not emphasis (2 * 3 * 4).
    assert answer_spans("2 * 3 * 4") == (Segment(text="2 * 3 * 4"),)


def test_answer_spans_task_list_checkboxes() -> None:
    """``- [x]`` / ``- [ ]`` render as task-list glyphs (green done / dim
    pending) rather than leaking a raw ``• [x]``; a plain bullet is
    unaffected."""
    assert answer_spans("- [x] shipped\n- [ ] todo\n- plain") == (
        Segment(text="✓ ", style_token="green"),
        Segment(text="shipped"),
        Segment(text="\n"),
        Segment(text="☐ ", style_token="dim"),
        Segment(text="todo"),
        Segment(text="\n"),
        Segment(text="• ", style_token="dim"),
        Segment(text="plain"),
    )


def test_answer_spans_markdown_link_carries_osc8_target() -> None:
    """A Markdown link keeps its teal text + dim ``(url)`` but both runs now
    carry a ``link`` target so the terminal paints a real OSC 8 hyperlink."""
    assert answer_spans("see [docs](https://example.com/g) now") == (
        Segment(text="see "),
        Segment(text="docs", style_token="teal", link="https://example.com/g"),
        Segment(
            text=" (https://example.com/g)",
            style_token="dimmer",
            link="https://example.com/g",
        ),
        Segment(text=" now"),
    )


def test_answer_spans_bare_url_collapses_to_hyperlink() -> None:
    """A bare URL collapses into one teal OSC 8 hyperlink; trailing sentence
    punctuation stays outside the link target."""
    assert answer_spans("visit https://amplifier.dev. thanks") == (
        Segment(text="visit "),
        Segment(text="https://amplifier.dev", style_token="teal", link="https://amplifier.dev"),
        Segment(text=". thanks"),
    )


def test_visible_length_holds_back_trailing_table() -> None:
    # Trailing table run (with streaming-newline artifact) is withheld.
    assert visible_length(["Results:", "| a | b |", "| 1 | 2 |"]) == 1
    assert visible_length(["Results:", "| a | b |", ""]) == 1
    # No table → everything paints.
    assert visible_length(["Results:", "done"]) == 2
    # A paragraph break after the table completes it → paintable.
    assert visible_length(["Results:", "| a | b |", "", "Done"]) == 4


def test_streaming_spans_commit_complete_lines_only() -> None:
    spans = streaming_spans("# Result\nRun `pytest` — **done**.\npartial **mar")
    assert spans == (
        Segment(text="Result", style_token="bright", bold=True),
        Segment(text="\n"),
        Segment(text="\n"),
        Segment(text="Run "),
        Segment(text="pytest", style_token="teal"),
        Segment(text=" — "),
        Segment(text="done", style_token="bright", bold=True),
        Segment(text="."),
        Segment(text="\n"),
        Segment(text="partial **mar"),
    )


def test_streaming_spans_hold_table_and_track_open_fence() -> None:
    table = streaming_spans("Results:\n| Check | State |\n| tests | pass |")
    assert table == (Segment(text="Results:"),)

    code = streaming_spans("```python\nprint('ok')\nret")
    assert code[-1] == Segment(text="  ret", style_token="teal")
    assert all("```" not in segment.text for segment in code)


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


def test_markup_for_caps_revealed_stream_to_max_lines() -> None:
    """A revealed root stream shows only the last :data:`MAX_ROOT_LINES` lines —
    the box is a peek; the durable text arrives on the consolidated Answer."""
    src = "\n".join(f"row{index}" for index in range(20))
    out = LiveTail._markup_for(src, "thinking", MAX_ROOT_LINES)
    assert "row19" in out  # newest line kept
    assert "row0\n" not in out and not out.endswith("row0")  # oldest trimmed
    inner = out[len("[italic $dim]") : -len("[/]")]
    assert inner.count("\n") == MAX_ROOT_LINES - 1  # exactly the last N lines


def test_markup_for_without_cap_is_unchanged() -> None:
    """Omitting ``max_lines`` reproduces the pre-reveal rendering byte-for-byte."""
    src = "# Head\nRun `pytest`\nbody text"
    assert LiveTail._markup_for(src, "text") == LiveTail._markup_for(src, "text", None)


def test_toggle_reveal_returns_state_and_persists() -> None:
    """Reveal is a session preference: it flips and sticks (default hidden)."""
    tail = LiveTail()
    assert tail.revealed is False  # default hidden
    assert tail.toggle_reveal() is True
    assert tail.revealed is True
    assert tail.toggle_reveal() is False
    assert tail.revealed is False


@pytest.mark.asyncio
async def test_hidden_root_stream_paints_peek_hint(monkeypatch) -> None:
    """Default-hidden: an open root stream paints a one-line peek, not content."""
    app = TailHarness()
    async with app.run_test(size=(80, 24)) as pilot:
        tail = _tail(app)
        painted: list[str] = []
        monkeypatch.setattr(tail, "update", painted.append)
        tail.open_stream(block_type="thinking")
        tail.feed("secret line one\nsecret line two")
        await pilot.pause(0.1)
        assert tail.revealed is False
        assert painted[-1] == tail._reveal_hint()
        assert "click to show" in painted[-1]
        assert "secret line" not in painted[-1]  # content stays hidden


@pytest.mark.asyncio
async def test_revealed_root_stream_paints_capped_content(monkeypatch) -> None:
    """After reveal, an open stream paints the last few lines of real content."""
    app = TailHarness()
    async with app.run_test(size=(80, 24)) as pilot:
        tail = _tail(app)
        tail.toggle_reveal()  # user shows the box
        painted: list[str] = []
        monkeypatch.setattr(tail, "update", painted.append)
        tail.open_stream(block_type="thinking")
        tail.feed("\n".join(f"line{index}" for index in range(10)))
        await pilot.pause(THROTTLE_SECONDS * 4)
        assert tail.revealed is True
        assert "line9" in painted[-1]  # newest content shown
        assert "click to show" not in painted[-1]  # not the hint


@pytest.mark.asyncio
async def test_completed_stream_lines_use_final_markup_before_consolidation() -> None:
    app = TailHarness()
    async with app.run_test(size=(80, 24)) as pilot:
        tail = _tail(app)
        tail.open_stream()
        tail.feed("# Result\nRun `pytest`\npartial **mar")
        await pilot.pause(0.1)
        markup = tail._markup()
        assert "# Result" not in markup
        assert "[bold $bright]Result[/]" in markup
        assert "[$teal]pytest[/]" in markup
        assert "partial **mar" in markup


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


@pytest.mark.asyncio
async def test_long_stream_render_keeps_event_loop_responsive() -> None:
    """Large markdown parsing is coalesced off the Textual event loop."""
    import time

    app = TailHarness()
    async with app.run_test(size=(80, 24)) as pilot:
        tail = _tail(app)
        tail.open_stream()
        payload = "A **bold** line with `code`.\n" * (ASYNC_RENDER_THRESHOLD // 20)
        started = time.perf_counter()
        tail.feed(payload)
        assert time.perf_counter() - started < 0.05

        event_loop_ran = False

        def mark() -> None:
            nonlocal event_loop_ran
            event_loop_ran = True

        app.call_later(mark)
        await pilot.pause(0.05)
        assert event_loop_ran
        assert tail.source == payload


# -- lane mode (design doc D4: focused-lane live tail) --------------------------


def test_lane_tail_markup_gutters_dims_and_caps_at_three_lines() -> None:
    markup = lane_tail_markup("one\ntwo\nthree\nfour\n")
    assert markup == "[$dim]┆ two\n┆ three\n┆ four[/]"


def test_lane_tail_markup_escapes_and_handles_empty() -> None:
    assert lane_tail_markup("") == ""
    assert lane_tail_markup("   \n") == ""
    markup = lane_tail_markup("[red]not markup")
    assert markup.startswith("[$dim]")
    assert "┆ \\[red]not markup" in markup  # escaped — content is never interpreted


def test_labeled_lane_tail_markup_prefixes_first_line_only() -> None:
    """Rust: test_labeled_lane_tail_markup_prefixes_first_line_only — the
    main transcript's delegate tail leads its first rendered line with the
    lane's short label; the panel shape (empty label) stays byte-identical."""
    markup = lane_tail_markup("one\ntwo\nthree\nfour\n", label="explorer")
    assert markup == "[$dim]┆ explorer › two\n┆ three\n┆ four[/]"
    # Empty label degrades to the panel tail verbatim.
    assert lane_tail_markup("one\ntwo", label="") == lane_tail_markup("one\ntwo")
    assert lane_tail_markup("", label="explorer") == ""
    # Label content is escaped, never interpreted.
    assert "┆ \\[red]agent › hi" in lane_tail_markup("hi", label="[red]agent")


@pytest.mark.asyncio
async def test_lane_mode_yields_to_root_stream_and_clears() -> None:
    app = TailHarness()
    async with app.run_test():
        tail = _tail(app)
        tail.show_lane_tail("agent prose")
        assert tail.lane_mode
        tail.open_stream("text")  # root preempts instantly
        assert not tail.lane_mode
        tail.show_lane_tail("ignored while root streams")
        assert not tail.lane_mode  # refused: root owns the tail
        tail.feed("root text")
        tail.consolidate("blk-1")  # root stream closed
        tail.show_lane_tail("agent prose again")
        assert tail.lane_mode  # lanes may resume after the root goes idle
        tail.clear_lane_tail()
        assert not tail.lane_mode
