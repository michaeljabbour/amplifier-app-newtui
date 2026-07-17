"""``/copy`` — last-answer extraction for the clipboard.

Pure-logic tests over ``commands.copy``: the last clickable Answer's
span-joined text, skipping trailing non-Answer blocks and
``clickable=False`` answer-shaped lines (recap/agent-tree rows).
"""

from __future__ import annotations

from amplifier_app_newtui.commands.copy import last_answer_text
from amplifier_app_newtui.model.blocks import (
    Answer,
    Segment,
    ToolLine,
    UserLine,
)


def test_returns_span_joined_text_of_last_answer() -> None:
    blocks = (
        Answer(id="b1", spans=(Segment(text="first answer"),)),
        Answer(
            id="b2",
            spans=(
                Segment(text="The fix is in "),
                Segment(text="app.py", style_token="teal"),
                Segment(text=", shipped."),
            ),
        ),
    )
    assert last_answer_text(blocks) == "The fix is in app.py, shipped."


def test_skips_trailing_non_answer_blocks() -> None:
    blocks = (
        Answer(id="b1", spans=(Segment(text="the real answer"),)),
        UserLine(id="b2", text="thanks"),
        ToolLine(id="b3", summary="Ran ls", status="completed"),
    )
    assert last_answer_text(blocks) == "the real answer"


def test_skips_non_clickable_answers() -> None:
    blocks = (
        Answer(id="b1", spans=(Segment(text="true answer"),)),
        Answer(id="b2", spans=(Segment(text="recap-shaped line"),), clickable=False),
    )
    assert last_answer_text(blocks) == "true answer"


def test_returns_none_for_empty_blocks() -> None:
    assert last_answer_text(()) is None


def test_returns_none_when_no_answers() -> None:
    blocks = (
        UserLine(id="b1", text="hello"),
        ToolLine(id="b2", summary="Read blocks.py"),
    )
    assert last_answer_text(blocks) is None
