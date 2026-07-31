"""Client UX for the host `question` tool: option descriptions, multi-select,
and free-text answering surfaced in BOTH the pure renderer and the interactive
needs-you widget (HGT capability question-tool-client).

These pin the CLIENT delta: descriptions/multiple/custom flow through the
existing needs-you decision path (no new protocol op) and RENDER, and the
multi-select answer is the donor's comma-joined labels.
"""

from __future__ import annotations

from amplifier_app_tui.kernel import events as ev
from amplifier_app_tui.kernel.question import parse_questions
from amplifier_app_tui.model.blocks import (
    GLYPH_CHECKBOX_EMPTY,
    BlockIdAllocator,
    NeedsYouBlock,
    NeedsYouChoice,
    NeedsYouEntry,
)
from amplifier_app_tui.model.queues import NeedsYouQueue
from amplifier_app_tui.ui.app_support import needs_you_block
from amplifier_app_tui.ui.needs_you import (
    NeedsYouList,
    multi_answer,
    multi_chip_text,
    option_description_line,
)
from amplifier_app_tui.ui.transcript_render import render_block


def _plain(lines) -> list[str]:
    return ["".join(seg.text for seg in line) for line in lines]


def _question_item(**over):
    """A parked question-tool decision (multi-select + descriptions + custom)."""
    queue = NeedsYouQueue()
    return queue.defer(
        over.get("question", "Which merge strategy?"),
        over.get("reason", "merge-strategy"),
        choices=over.get("choices", ("Squash", "Rebase")),
        descriptions=over.get("descriptions", ("Combine into one commit", "Replay onto main")),
        multiple=over.get("multiple", True),
        custom=over.get("custom", True),
    )


# -- data-flow: the question tool's richer shape survives to the item ----------


def test_parse_questions_keeps_descriptions_multiple_custom() -> None:
    prompts = parse_questions(
        [
            {
                "question": "Pick a strategy",
                "header": "strategy",
                "multiple": True,
                "custom": False,
                "options": [
                    {"label": "Squash", "description": "one commit"},
                    {"label": "Rebase", "description": "replay onto main"},
                ],
            }
        ]
    )
    assert len(prompts) == 1
    p = prompts[0]
    assert p.labels == ("Squash", "Rebase")
    assert p.descriptions == ("one commit", "replay onto main")
    assert p.multiple is True
    assert p.custom is False


def test_defer_aligns_descriptions_and_drops_blank_labels() -> None:
    queue = NeedsYouQueue()
    item = queue.defer(
        "Q?",
        "hdr",
        choices=("A", "", "B"),
        descriptions=("desc-a", "desc-x", "desc-b"),
        multiple=True,
        custom=True,
    )
    # The blank label AND its aligned description are dropped together.
    assert item.choices == ("A", "B")
    assert item.descriptions == ("desc-a", "desc-b")
    assert item.multiple is True
    assert item.custom is True


def test_defer_without_descriptions_leaves_empty_tuple() -> None:
    queue = NeedsYouQueue()
    item = queue.defer("Q?", "hdr", choices=("A", "B"))
    assert item.descriptions == ()
    assert item.multiple is False
    assert item.custom is False


# -- item -> block: the entry carries the question-tool shape -------------------


def test_needs_you_block_carries_descriptions_multiple_custom() -> None:
    block = needs_you_block((_question_item(),), BlockIdAllocator())
    assert block is not None
    entry = block.items[0]
    assert entry.multiple is True
    assert entry.custom is True
    assert [c.description for c in entry.choices] == [
        "Combine into one commit",
        "Replay onto main",
    ]
    # The answer stays the bare label (donor answer contract).
    assert [c.answer for c in entry.choices] == ["Squash", "Rebase"]


# -- pure renderer: the donor option UX is visible -----------------------------


def test_render_shows_descriptions_checkbox_selectall_and_custom() -> None:
    block = needs_you_block((_question_item(),), BlockIdAllocator())
    lines = _plain(render_block(block, 200))
    joined = "\n".join(lines)
    # Multi-select hint on the question row + checkbox chips.
    assert "(select all that apply)" in joined
    assert f"[{GLYPH_CHECKBOX_EMPTY} Squash]" in joined
    assert f"[{GLYPH_CHECKBOX_EMPTY} Rebase]" in joined
    # Per-option descriptions as their own dim lines.
    assert "      Squash \u00b7 Combine into one commit" in lines
    assert "      Rebase \u00b7 Replay onto main" in lines
    # Free-text affordance (donor "Type your own answer").
    assert "      + type your own answer" in lines


def test_render_governance_decision_unchanged_regression() -> None:
    # A plain single-select decision (no descriptions/multiple/custom) must
    # render EXACTLY as before: labels-only chips, no checkbox, no extra lines.
    block = NeedsYouBlock(
        id="b1",
        items=(
            NeedsYouEntry(
                decision_id="decision-1",
                question="Push to fork mj/waypoint instead?",
                reason="outside trust boundary",
                choices=(NeedsYouChoice(label="yes \u00b7 push to fork", answer="push"),),
            ),
        ),
    )
    lines = _plain(render_block(block, 200))
    assert lines[1] == "  1 Push to fork mj/waypoint instead?  [yes \u00b7 push to fork]"
    assert lines[2] == "    why \u00b7 outside trust boundary"
    assert len(lines) == 3  # header + row + why -- nothing extra
    assert GLYPH_CHECKBOX_EMPTY not in "\n".join(lines)


# -- wire: the additive Notification fields parse ------------------------------


def test_notification_parses_question_tool_wire_fields() -> None:
    # parse_event validates the persisted UIEvent shape (kind-discriminated);
    # the additive fields must round-trip losslessly through the wire model.
    event = ev.parse_event(
        {
            "kind": "notification",
            "session_id": "s1",
            "level": "decision",
            "source": "needs_you",
            "decision_id": "decision-q1",
            "question": "Which strategy?",
            "reason": "strategy",
            "choices": ["Squash", "Rebase"],
            "descriptions": ["one commit", "replay onto main"],
            "multiple": True,
            "custom": True,
        }
    )
    assert isinstance(event, ev.Notification)
    assert event.descriptions == ("one commit", "replay onto main")
    assert event.multiple is True
    assert event.custom is True


def test_normalize_hook_notification_carries_question_fields() -> None:
    # The raw-hook normalize path (RealRuntime emits `user:notification`) must
    # also carry the additive question-tool detail onto the typed event.
    event = ev.normalize(
        "user:notification",
        {
            "message": "decision deferred to queue · Which strategy?",
            "level": "decision",
            "source": "needs_you",
            "decision_id": "decision-q1",
            "question": "Which strategy?",
            "reason": "strategy",
            "choices": ["Squash", "Rebase"],
            "descriptions": ["one commit", "replay onto main"],
            "multiple": True,
            "custom": True,
        },
    )
    assert isinstance(event, ev.Notification)
    assert event.descriptions == ("one commit", "replay onto main")
    assert event.multiple is True
    assert event.custom is True


# -- interactive widget: multi-select answer semantics -------------------------


def test_multi_select_toggle_then_submit_is_comma_joined() -> None:
    block = needs_you_block((_question_item(),), BlockIdAllocator())
    widget = NeedsYouList(block)
    item_id = block.items[0].decision_id
    widget.toggle_choice(item_id, 0)
    widget.toggle_choice(item_id, 1)
    widget.toggle_choice(item_id, 0)  # toggling twice removes it
    assert widget.selected_labels(item_id) == ("Rebase",)
    widget.toggle_choice(item_id, 0)
    assert widget.selected_labels(item_id) == ("Squash", "Rebase")
    # The submitted answer joins labels with ", " (donor multi-select contract).
    assert multi_answer(widget.selected_labels(item_id)) == "Squash, Rebase"


def test_multi_and_option_helpers_exact_strings() -> None:
    choice = NeedsYouChoice(label="Squash", answer="Squash", description="one commit")
    assert multi_chip_text(choice, selected=False) == f"[{GLYPH_CHECKBOX_EMPTY} Squash]"
    assert option_description_line(choice) == "      Squash \u00b7 one commit"
