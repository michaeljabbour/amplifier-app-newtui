"""Tests for the keymap-as-data table (ui/keymap.py)."""

from __future__ import annotations

import pytest

from amplifier_app_newtui.ui.keymap import (
    ALL_CONTEXTS,
    COMPOSER_PLACEHOLDER,
    ESC_BACKTRACK_WINDOW_SECONDS,
    ESC_CHAIN,
    FOOTER_HINTS,
    KEYMAP,
    NO_APPROVAL,
    Binding,
    bindings_for,
    hint_label,
    validate,
)


def test_keymap_validates_clean() -> None:
    validate()


def test_required_actions_present_with_expected_keys() -> None:
    by_action: dict[str, list[Binding]] = {}
    for binding in KEYMAP:
        by_action.setdefault(binding.action, []).append(binding)
    assert by_action["cycle_mode"][0].keys == ("shift+tab",)
    assert by_action["toggle_lanes"][0].keys == ("ctrl+t",)
    assert by_action["show_ledger"][0].keys == ("ctrl+l",)
    assert by_action["show_needs_you"][0].keys == ("ctrl+y",)
    assert by_action["open_rewind"][0].keys == ("ctrl+r",)
    assert by_action["submit"][0].keys == ("enter",)
    assert by_action["insert_newline"][0].keys == ("ctrl+j", "ctrl+enter")
    assert by_action["history_prev"][0].keys == ("up",)
    assert by_action["history_next"][0].keys == ("down",)


def test_shift_enter_with_alt_enter_fallback() -> None:
    queue = [b for b in KEYMAP if b.action == "queue_message"]
    assert len(queue) == 2
    primary = next(b for b in queue if not b.fallback)
    fallback = next(b for b in queue if b.fallback)
    assert primary.keys == ("shift+enter",)
    assert fallback.keys == ("alt+enter",)
    # The advertised label defaults to the primary chord …
    assert hint_label("queue_message") == "shift+enter"
    # … and the terminal probe swaps it via overrides on legacy terminals.
    assert hint_label("queue_message", {"queue_message": "alt+enter"}) == "alt+enter"


def test_esc_chain_priority_order_per_spec() -> None:
    """DESIGN-SPEC §5: lane-focus → palette → rewind → lanes → interrupt."""
    assert [context for context, _ in ESC_CHAIN] == [
        "lane_focus",
        "palette",
        "rewind",
        "lanes",
        "running",
    ]
    # Every chained action really is an escape binding in that context.
    for context, action in ESC_CHAIN:
        bindings = [b for b in bindings_for(context) if b.action == action]
        assert bindings, (context, action)
        assert "escape" in bindings[0].keys
    assert ESC_BACKTRACK_WINDOW_SECONDS == 0.75


def test_footer_hints_exact_spec_strings() -> None:
    assert FOOTER_HINTS["approval"] == "arrows select · enter confirm · esc deny"
    assert (
        FOOTER_HINTS["lane_focus"]
        == "esc back to parent · transcript is the subagent's own"
    )
    assert FOOTER_HINTS["palette"] == "↑↓ select · enter run · esc close"
    assert FOOTER_HINTS["mention"] == "↑↓ select · enter/tab insert · esc close"
    assert FOOTER_HINTS["running"] == "esc interrupt · enter steer · shift+enter queue"
    assert FOOTER_HINTS["idle"] == "↑ history · ctrl+j newline · / commands"


def test_composer_placeholder_exact() -> None:
    assert COMPOSER_PLACEHOLDER == (
        "Message Amplifier…  "
        "( ↑ history · ctrl+j newline · enter send · / commands )"
    )


def test_validate_rejects_conflicts() -> None:
    conflicted = KEYMAP + (
        Binding(
            action="something_else",
            keys=("shift+tab",),
            label="shift+tab",
            contexts=frozenset({"idle"}),
        ),
    )
    with pytest.raises(ValueError, match="claimed by both"):
        validate(conflicted)


def test_validate_rejects_missing_label() -> None:
    bad = (
        Binding(action="x", keys=("ctrl+q",), label="", contexts=frozenset({"idle"})),
    )
    with pytest.raises(ValueError, match="display label"):
        validate(bad)


def test_hint_label_unknown_action_fails_loudly() -> None:
    with pytest.raises(KeyError):
        hint_label("no_such_action")


def test_open_palette_is_display_only() -> None:
    binding = next(b for b in KEYMAP if b.action == "open_palette")
    assert binding.keys == ()
    assert binding.label == "/"


def test_contexts_are_known() -> None:
    for binding in KEYMAP:
        assert binding.contexts <= ALL_CONTEXTS


def test_file_mention_keys_live_in_the_keymap_table() -> None:
    actions = {binding.action for binding in bindings_for("mention")}
    assert {"mention_up", "mention_down", "mention_accept", "mention_close"} <= actions


def test_approval_context_suppresses_global_chords() -> None:
    approval_actions = {b.action for b in bindings_for("approval")}
    assert "cycle_mode" not in approval_actions
    assert "queue_message" not in approval_actions
    assert {"approval_prev", "approval_next", "approval_confirm", "approval_deny"} <= (
        approval_actions
    )


def test_cycle_tail_is_bound_to_ctrl_o_everywhere_but_approval() -> None:
    binding = next(b for b in KEYMAP if b.action == "cycle_tail")
    assert binding.keys == ("ctrl+o",)
    assert binding.contexts == NO_APPROVAL


def test_approval_defer_parks_on_ctrl_y_in_approval_context_only() -> None:
    """Issue #41: ctrl-y parks the live ticket into the needs-you queue.

    The chord lives in the approval context only — globally ctrl-y is
    show_needs_you (NO_APPROVAL), and the bar owns the keyboard while
    open, so the same key means "defer THIS ticket" there. validate()
    accepts the split because no single (key, context) is double-claimed.
    """
    defer = next(b for b in KEYMAP if b.action == "approval_defer")
    assert defer.keys == ("ctrl+y",)
    assert defer.contexts == frozenset({"approval"})
    show = next(b for b in KEYMAP if b.action == "show_needs_you")
    assert show.keys == ("ctrl+y",)
    assert "approval" not in show.contexts
    validate()  # the ctrl-y split does not trip the conflict guard
