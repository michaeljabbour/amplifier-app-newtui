"""Tests for the keymap-as-data table (ui/keymap.py)."""

from __future__ import annotations

import pytest

from amplifier_app_tui.ui.keymap import (
    ACTION_HELP,
    ALL_CONTEXTS,
    COMPOSER_PLACEHOLDER,
    ESC_BACKTRACK_WINDOW_SECONDS,
    ESC_CHAIN,
    FOOTER_HINTS,
    HELP_ACTIONS,
    KEYMAP,
    NO_APPROVAL,
    Binding,
    bindings_for,
    help_rows,
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
    assert by_action["cycle_effort"][0].keys == ("ctrl+b",)  # HGT effort cycle
    assert by_action["toggle_lanes"][0].keys == ("ctrl+t",)
    assert by_action["show_ledger"][0].keys == ("ctrl+l",)
    assert by_action["show_needs_you"][0].keys == ("ctrl+y",)
    assert by_action["open_rewind"][0].keys == ("ctrl+r",)
    assert by_action["submit"][0].keys == ("enter",)
    assert by_action["insert_newline"][0].keys == ("ctrl+j", "ctrl+enter")
    assert by_action["history_prev"][0].keys == ("up",)
    assert by_action["history_next"][0].keys == ("down",)
    assert by_action["recall_queued"][0].keys == ("alt+up",)
    assert by_action["recall_queued"][0].contexts == frozenset({"idle", "running"})


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
    """DESIGN-SPEC §5 (extended by S2): lane-focus → palette → rewind →
    sessions → lanes → interrupt."""
    assert [context for context, _ in ESC_CHAIN] == [
        "lane_focus",
        "palette",
        "rewind",
        "sessions",
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
    assert FOOTER_HINTS["lane_focus"] == "esc back to parent · transcript is the subagent's own"
    assert FOOTER_HINTS["palette"] == "↑↓ select · enter run · esc close"
    assert FOOTER_HINTS["mention"] == "↑↓ select · enter/tab insert · esc close"
    assert FOOTER_HINTS["needs_you"] == "enter submit · ctrl-j newline · esc cancel"
    assert FOOTER_HINTS["running"] == "esc interrupt · enter steer · shift+enter queue"


def test_footer_idle_hint_is_empty() -> None:
    """Item D4 (AC2/AC3): the generic idle reminder no longer occupies the
    footer every frame — it moved to :data:`COMPOSER_PLACEHOLDER` and the
    ``/keys`` command (:func:`help_rows`)."""
    assert FOOTER_HINTS["idle"] == ""


def test_composer_placeholder_exact() -> None:
    assert COMPOSER_PLACEHOLDER == (
        "Message Amplifier…  ( ↑ history · ctrl+j newline · enter send · / commands )"
    )


# -- /keys reference (item D4: shortcut definitions render from ONE shared
# source, so trimming the footer's idle hint never costs discoverability) ----


def test_help_rows_labels_come_from_the_keymap_live() -> None:
    """Every label is exactly what :func:`hint_label` returns — never a
    hand-copied string that could drift from the bound chord."""
    rows = help_rows()
    assert dict(rows)["ctrl-r"] == (
        "open pre-prompt checkpoints to restore code, conversation, or both "
        "(restoring mid-turn interrupts it first)"
    )
    for action in HELP_ACTIONS:
        assert hint_label(action) in dict(rows)


def test_help_rows_covers_the_actions_the_old_idle_hint_taught() -> None:
    """The chords the removed generic hint advertised (history, newline,
    rewind, the palette) are still discoverable — just moved, not dropped."""
    labels = {label for label, _ in help_rows()}
    assert {"enter", "ctrl+j", "↑", "ctrl-r", "/"} <= labels


def test_help_rows_omits_overlay_only_navigation() -> None:
    """Chords that only mean something while an overlay owns the keyboard
    (palette/mention/lane/rewind/approval/evidence navigation) are left to
    that overlay's own context-sensitive footer hint — not duplicated here."""
    actions = set(HELP_ACTIONS)
    assert actions.isdisjoint(
        {
            "palette_up",
            "palette_down",
            "mention_up",
            "mention_down",
            "lane_up",
            "lane_down",
            "rewind_prev",
            "rewind_next",
            "approval_prev",
            "approval_next",
            "evidence_prev",
            "evidence_next",
        }
    )


def test_action_help_has_an_entry_for_every_help_action() -> None:
    for action in HELP_ACTIONS:
        assert ACTION_HELP[action].strip()


def test_open_rewind_help_states_mid_turn_interrupt_behavior() -> None:
    """S1 AC4: the static /keys help for rewind must say what happens to
    an in-progress turn, not just what the checkpoint restores. Restoring
    mid-turn interrupts the turn first and waits for it to close out before
    the restore itself runs (app_support.confirm_restore's interrupt-first
    path,
    exercised end-to-end by test_flow_rewind.py's
    test_restore_during_running_turn_interrupts_then_restores_before_prompt).

    Guards content, not just presence -- mirrors D3 AC4's
    test_clear_palette_desc_states_scope_per_d3_ac4 for /clear: a future
    edit could leave the entry non-empty but drift back to an
    under-described one-liner that silently re-opens the gap.
    """
    help_text = ACTION_HELP["open_rewind"]
    assert "interrupt" in help_text, "must say restoring mid-turn interrupts the turn"
    assert "mid-turn" in help_text, "must call out the in-progress-turn case explicitly"
    assert {"code", "conversation", "both"} <= set(help_text.replace(",", "").split())


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
    bad = (Binding(action="x", keys=("ctrl+q",), label="", contexts=frozenset({"idle"})),)
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


def test_stash_prompt_bound_to_ctrl_s_idle_and_running() -> None:
    """prompt-stash (HGT): ctrl+s stashes the draft, active while composing or
    while a turn runs; the new chord does not collide (validate stays clean)."""
    binding = next(b for b in KEYMAP if b.action == "stash_prompt")
    assert binding.keys == ("ctrl+s",)
    assert binding.contexts == frozenset({"idle", "running"})
    assert binding.label == "ctrl-s stash"
    assert len(binding.label) <= 32
    validate()


def test_return_to_answer_is_bound_to_ctrl_f_everywhere_but_approval() -> None:
    """AC2 return-to-answer action (compliance 2026-08-02 B1): ctrl+f is free
    in both the keymap table and ComposerInput's TextArea bindings (unlike
    ctrl+a/ctrl+e, which TextArea claims for home/end of line), so no
    composer-side interception is needed for it to reach the app."""
    binding = next(b for b in KEYMAP if b.action == "return_to_answer")
    assert binding.keys == ("ctrl+f",)
    assert binding.contexts == NO_APPROVAL
    assert binding.label == "ctrl-f answer"
    assert len(binding.label) <= 32
    validate()


def test_toggle_plan_overflow_is_bound_to_ctrl_h_everywhere_but_approval() -> None:
    """S7 gap 1 (keyboard reachability): ctrl+h reaches AND toggles the plan
    overflow control directly -- mirroring how ctrl+n's plan_drilldown
    already works globally without ever moving focus off the composer."""
    binding = next(b for b in KEYMAP if b.action == "toggle_plan_overflow")
    assert binding.keys == ("ctrl+h",)
    assert binding.contexts == NO_APPROVAL
    assert binding.label == "ctrl-h plan"
    assert len(binding.label) <= 32
    validate()


def test_toggle_plan_overflow_is_taught_in_keys_reference() -> None:
    """Requirement: a new binding lives in the data-driven keymap table so
    /keys teaches it automatically -- never a hand-copied help string."""
    assert "toggle_plan_overflow" in HELP_ACTIONS
    rows = dict(help_rows())
    assert rows["ctrl-h plan"] == "expand or collapse the plan panel's hidden rows"
