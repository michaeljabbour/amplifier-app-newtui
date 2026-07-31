"""Reducer routing for the durable Thinking transcript block (issue #129).

The loop-streaming runtime brackets a thinking block with
``content_block:start`` + ``content_block:end`` (no token deltas). The
reducer opens a collapsed Thinking block on start and populates it in place
on end — reading ``block["thinking"]`` then ``block["text"]`` — degrading
honestly when core withholds the prose (empty text).

Offline: fake events straight into the reducer, no Textual.
"""

from __future__ import annotations

from amplifier_app_tui.kernel import events as ev
from amplifier_app_tui.model.blocks import BlockIdAllocator, Thinking
from amplifier_app_tui.model.lanes import LaneRegistry
from amplifier_app_tui.model.turn import OutcomeLedger
from amplifier_app_tui.ui.reducer import TranscriptReducer

from .test_ui_reducer_outcomes import FakeHost


def make_reducer() -> tuple[TranscriptReducer, FakeHost]:
    host = FakeHost()
    reducer = TranscriptReducer(
        host,
        allocator=BlockIdAllocator(),
        ledger=OutcomeLedger(),
        lanes=LaneRegistry(),
    )
    return reducer, host


def _thinking(host: FakeHost) -> list[Thinking]:
    return [b for b in host.blocks if isinstance(b, Thinking)]


def test_start_then_end_populates_one_collapsed_block_in_place() -> None:
    reducer, host = make_reducer()
    reducer.handle(ev.PromptSubmit(session_id="root", prompt="think", ts=1.0))
    reducer.handle(ev.ContentBlockStart(session_id="root", block_type="thinking", ts=2.0))
    reducer.handle(
        ev.ContentBlockEnd(
            session_id="root",
            block_type="thinking",
            block={"type": "thinking", "thinking": "weigh A vs B\npick A"},
            ts=2.5,
        )
    )
    blocks = _thinking(host)
    assert len(blocks) == 1
    assert blocks[0].text == "weigh A vs B\npick A"
    assert blocks[0].expanded is False  # default collapsed, Claude-Code style


def test_thinking_prefers_thinking_field_over_text() -> None:
    reducer, host = make_reducer()
    reducer.handle(ev.PromptSubmit(session_id="root", prompt="think", ts=1.0))
    reducer.handle(ev.ContentBlockStart(session_id="root", block_type="thinking", ts=2.0))
    reducer.handle(
        ev.ContentBlockEnd(
            session_id="root",
            block_type="thinking",
            block={"type": "thinking", "thinking": "real reasoning", "text": "ignored"},
            ts=2.5,
        )
    )
    assert _thinking(host)[0].text == "real reasoning"


def test_thinking_falls_back_to_text_key() -> None:
    reducer, host = make_reducer()
    reducer.handle(ev.PromptSubmit(session_id="root", prompt="think", ts=1.0))
    reducer.handle(ev.ContentBlockStart(session_id="root", block_type="thinking", ts=2.0))
    reducer.handle(
        ev.ContentBlockEnd(
            session_id="root",
            block_type="thinking",
            block={"type": "thinking", "text": "text-key reasoning"},
            ts=2.5,
        )
    )
    assert _thinking(host)[0].text == "text-key reasoning"


def test_withheld_thinking_keeps_the_block_with_empty_text() -> None:
    """Honest degradation: core withheld the prose (visibility LLM_ONLY),
    the payload arrives empty — the block survives rather than vanishing."""
    reducer, host = make_reducer()
    reducer.handle(ev.PromptSubmit(session_id="root", prompt="think", ts=1.0))
    reducer.handle(ev.ContentBlockStart(session_id="root", block_type="thinking", ts=2.0))
    reducer.handle(
        ev.ContentBlockEnd(
            session_id="root",
            block_type="thinking",
            block={"type": "thinking"},  # no thinking/text: withheld
            ts=2.5,
        )
    )
    blocks = _thinking(host)
    assert len(blocks) == 1
    assert blocks[0].text == ""


def test_thinking_end_without_start_appends_defensively() -> None:
    """Non-streaming provider (no start): the end alone still lands a block."""
    reducer, host = make_reducer()
    reducer.handle(ev.PromptSubmit(session_id="root", prompt="think", ts=1.0))
    reducer.handle(
        ev.ContentBlockEnd(
            session_id="root",
            block_type="thinking",
            block={"type": "thinking", "thinking": "standalone"},
            ts=2.0,
        )
    )
    blocks = _thinking(host)
    assert len(blocks) == 1
    assert blocks[0].text == "standalone"


def test_thinking_does_not_bleed_into_answer_channel() -> None:
    """A thinking content block must never be treated as durable answer text."""
    from amplifier_app_tui.model.blocks import Answer

    reducer, host = make_reducer()
    reducer.handle(ev.PromptSubmit(session_id="root", prompt="think", ts=1.0))
    reducer.handle(ev.ContentBlockStart(session_id="root", block_type="thinking", ts=2.0))
    reducer.handle(
        ev.ContentBlockEnd(
            session_id="root",
            block_type="thinking",
            block={"type": "thinking", "thinking": "private"},
            ts=2.5,
        )
    )
    reducer.handle(
        ev.ContentBlockEnd(
            session_id="root",
            block_type="text",
            block={"type": "text", "text": "The answer."},
            ts=3.0,
        )
    )
    answers = [b for b in host.blocks if isinstance(b, Answer)]
    assert [("".join(s.text for s in a.spans)) for a in answers] == ["The answer."]
    assert "private" not in "".join("".join(s.text for s in a.spans) for a in answers)
