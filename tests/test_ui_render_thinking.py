"""Thinking renderer: pure (block, width) → lines (issue #129).

Collapsed is one dim summary line; expanded shows the reasoning prose; a
withheld (empty-text) block degrades to a single honest line that never
expands.
"""

from __future__ import annotations

from amplifier_app_newtui.model.blocks import (
    GLYPH_CHEVRON_COLLAPSED,
    GLYPH_CHEVRON_EXPANDED,
    Thinking,
)
from amplifier_app_newtui.ui.segments import lines_plain
from amplifier_app_newtui.ui.transcript import render_block


def _plain(block: Thinking, width: int = 97) -> list[str]:
    return [lines_plain([line]) for line in render_block(block, width)]


def test_collapsed_single_line_exact() -> None:
    block = Thinking(id="b1", text="one line only")
    assert _plain(block) == [
        f"{GLYPH_CHEVRON_COLLAPSED} thinking · 1 line · ctrl-g/click to expand"
    ]


def test_collapsed_pluralizes_line_count() -> None:
    block = Thinking(id="b1", text="first\nsecond\nthird")
    assert _plain(block) == [
        f"{GLYPH_CHEVRON_COLLAPSED} thinking · 3 lines · ctrl-g/click to expand"
    ]


def test_expanded_shows_prose_under_header() -> None:
    block = Thinking(id="b1", text="weigh options\npick the safe one", expanded=True)
    lines = _plain(block)
    assert lines[0] == f"{GLYPH_CHEVRON_EXPANDED} thinking"
    assert lines[1] == "  weigh options"
    assert lines[2] == "  pick the safe one"
    assert len(lines) == 3


def test_expanded_body_is_dim_italic() -> None:
    block = Thinking(id="b1", text="reasoning", expanded=True)
    body = render_block(block, 97)[1]
    assert body[0].style_token == "dim"
    assert body[0].italic is True


def test_withheld_thinking_degrades_honestly() -> None:
    """Empty text (core withheld the prose) → one honest line, no crash."""
    block = Thinking(id="b1", text="")
    assert _plain(block) == ["· thinking · (content withheld by provider)"]


def test_withheld_thinking_ignores_expanded_flag() -> None:
    """An expanded withheld block still renders the single withheld line."""
    block = Thinking(id="b1", text="", expanded=True)
    assert _plain(block) == ["· thinking · (content withheld by provider)"]


def test_block_round_trips_through_json() -> None:
    """The discriminated union serializes/deserializes losslessly (replay)."""
    from amplifier_app_newtui.model.blocks import TranscriptBlock
    from pydantic import TypeAdapter

    adapter = TypeAdapter(TranscriptBlock)
    block = Thinking(id="b7", text="a\nb", expanded=True)
    restored = adapter.validate_json(adapter.dump_json(block))
    assert restored == block
