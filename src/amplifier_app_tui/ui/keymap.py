"""Keymap as data: one table feeding Textual bindings AND footer hints.

Ported pattern from amplifier-app-cli ``ui/key_bindings_table.py`` (itself
after codex ``keymap.rs``): every binding knows its Textual key chord(s),
its on-screen hint label, and the UI contexts it is active in. Because
both the key handlers and the footer read the same table, the keys that
work and the keys the UI advertises can never drift.

Shift+Enter needs the kitty keyboard protocol (Textual >= 8.2.6); on
legacy terminals the ``fallback=True`` alt+enter chord is the working
alternative and :func:`hint_label` swaps the advertised label via
overrides after the terminal probe (DESIGN-SPEC §12).

Esc precedence is specified as a table, not emergent behavior (codex
lesson): :data:`ESC_CHAIN` is the priority order from DESIGN-SPEC §5.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict

# UI contexts a binding can be active in (spec §2/§5 surfaces).
Context = Literal[
    "idle",  # composer focused, no turn running
    "running",  # a turn is executing
    "palette",  # command palette strip open
    "mention",  # workspace-file autocomplete open
    "lanes",  # agent lanes panel open
    "lane_focus",  # a subagent lane is focused (child transcript shown)
    "rewind",  # rewind picker strip open
    "sessions",  # sessions picker strip open (S2: interactive session table)
    "approval",  # approval bar replaces the composer
    "needs_you",  # needs-you block focused
    "evidence",  # evidence block open
]

ALL_CONTEXTS: frozenset[Context] = frozenset(
    (
        "idle",
        "running",
        "palette",
        "mention",
        "lanes",
        "lane_focus",
        "rewind",
        "sessions",
        "approval",
        "needs_you",
        "evidence",
    )
)

# The approval bar owns the keyboard while visible; most global chords
# are suppressed under it.
NO_APPROVAL: frozenset[Context] = frozenset(ALL_CONTEXTS - {"approval"})

_MAX_LABEL_CHARS = 32


class Binding(BaseModel):
    """One key chord bound to a named action in a set of UI contexts.

    - ``action``: stable action id the app dispatches on.
    - ``keys``: Textual key names (e.g. ``"shift+tab"``, ``"ctrl+t"``).
      Multiple entries for one action are alternates.
    - ``label``: hint text advertised for this chord; the first labeled
      table entry per action wins (see :func:`hint_label`).
    - ``contexts``: UI states the binding is active in.
    - ``fallback``: True for legacy-terminal alternates (alt+enter for
      shift+enter) — registered always, advertised only when the terminal
      probe says the primary chord cannot arrive.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: str
    keys: tuple[str, ...]
    label: str
    contexts: frozenset[Context]
    fallback: bool = False


def _b(
    action: str,
    keys: tuple[str, ...],
    label: str,
    contexts: frozenset[Context],
    *,
    fallback: bool = False,
) -> Binding:
    return Binding(action=action, keys=keys, label=label, contexts=contexts, fallback=fallback)


_PALETTE: frozenset[Context] = frozenset({"palette"})
_MENTION: frozenset[Context] = frozenset({"mention"})
_LANES: frozenset[Context] = frozenset({"lanes"})
_LANE_FOCUS: frozenset[Context] = frozenset({"lane_focus"})
_REWIND: frozenset[Context] = frozenset({"rewind"})
_SESSIONS: frozenset[Context] = frozenset({"sessions"})
_APPROVAL: frozenset[Context] = frozenset({"approval"})
_EVIDENCE: frozenset[Context] = frozenset({"evidence"})
_RUNNING: frozenset[Context] = frozenset({"running"})
_IDLE: frozenset[Context] = frozenset({"idle"})

KEYMAP: tuple[Binding, ...] = (
    # Submission / steering / queueing (spec §5).
    _b("submit", ("enter",), "enter", _IDLE),
    _b("steer", ("enter",), "enter", _RUNNING),
    _b("insert_newline", ("ctrl+j", "ctrl+enter"), "ctrl+j", NO_APPROVAL),
    _b("history_prev", ("up",), "↑", frozenset({"idle", "running"})),
    _b("history_next", ("down",), "↓", frozenset({"idle", "running"})),
    _b("queue_message", ("shift+enter",), "shift+enter", NO_APPROVAL),
    _b("queue_message", ("alt+enter",), "alt+enter", NO_APPROVAL, fallback=True),
    # Mode & permission cycles (independent controls, ADR-0005 amendment).
    _b("cycle_mode", ("shift+tab",), "shift+tab", NO_APPROVAL),
    _b("cycle_permission", ("ctrl+p",), "ctrl-p", NO_APPROVAL),
    # Reasoning-effort tier cycle (HGT: donor variant.cycle; the donor chord
    # ctrl+t is taken by toggle_lanes, and ctrl+e is the external editor's donor
    # chord — claimed by open_external_editor below). ctrl+b ("boost") is free;
    # advances one tier in the canonical ring (xhigh wraps to none).
    _b("cycle_effort", ("ctrl+b",), "ctrl-b effort", NO_APPROVAL),
    # Panels / pickers.
    _b("toggle_lanes", ("ctrl+t",), "ctrl-t", NO_APPROVAL),
    _b("cycle_tail", ("ctrl+o",), "ctrl-o", NO_APPROVAL),
    # Compose the draft in $VISUAL/$EDITOR (ctrl+e is the donor's own
    # editor chord and is free in both clients; ctrl+g/ctrl+o are already
    # taken here by toggle_thinking/cycle_tail). Suspends the TUI, opens a
    # temp .md seeded with the draft, reads it back normalized.
    _b("open_external_editor", ("ctrl+e",), "ctrl-e edit", NO_APPROVAL),
    # Show/hide the root stream box (thinking/response peek). Advertised
    # only while a turn runs — that is the only time a live box exists.
    _b("toggle_thinking", ("ctrl+g",), "ctrl-g think", _RUNNING),
    _b("show_ledger", ("ctrl+l",), "ctrl-l", NO_APPROVAL),
    _b("show_needs_you", ("ctrl+y",), "ctrl-y", NO_APPROVAL),
    _b("open_rewind", ("ctrl+r",), "ctrl-r", NO_APPROVAL),
    # Return to the current/most-recent turn's final-answer start anchor
    # (AC2, compliance 2026-08-02 B1). ctrl+f is free in the global table
    # AND in ComposerInput's TextArea bindings (unlike ctrl+a/ctrl+e, which
    # TextArea claims for home/end of line) -- so, unlike open_external_editor's
    # ctrl+e, no composer-side interception is needed for it to reach here.
    _b("return_to_answer", ("ctrl+f",), "ctrl-f answer", NO_APPROVAL),
    # Plan-panel drilldown: while the ambient plan strip is visible, ctrl+n
    # cycles its row window default → +2 → +3 → back (ctrl+n is claimed by
    # neither the app tables nor Textual's TextArea defaults).
    _b("plan_drilldown", ("ctrl+n",), "ctrl-n", NO_APPROVAL),
    # S7 gap 1 (keyboard reachability): Enter/Space only ACTIVATE the plan
    # overflow control once it already has focus -- nothing in this table
    # gave a keyboard-only user a way to reach it in the first place (Tab
    # is not a general focus chain here: mention_accept/approval_next
    # claim plain tab, shift+tab is cycle_mode), and the composer
    # intentionally keeps focus so typing always steers (the same call
    # ui/transcript.py's FocusHeader docstring records for S6, which
    # deliberately did NOT make that header Tab-reachable either). ctrl+h
    # follows ctrl+n's own idiom for this exact panel instead: a dedicated
    # global chord that toggles the SAME state Enter/Space/click do,
    # directly, so the composer never loses focus.
    _b("toggle_plan_overflow", ("ctrl+h",), "ctrl-h plan", NO_APPROVAL),
    # Prompt-stash (HGT from opencode): stash the in-progress draft. The
    # save direction MUST be a keybind — typing a "/stash" command would make
    # the composer text the palette filter, clobbering the very draft it means
    # to save. Recall is the /unstash + /stashes commands (composer is empty
    # then, so a command is safe). ctrl+s survives raw mode (IXON disabled).
    _b("stash_prompt", ("ctrl+s",), "ctrl-s stash", frozenset({"idle", "running"})),
    # In-panel navigation.
    _b("palette_up", ("up",), "↑↓", _PALETTE),
    _b("palette_down", ("down",), "↑↓", _PALETTE),
    _b("palette_run", ("enter",), "enter", _PALETTE),
    _b("mention_up", ("up",), "↑↓", _MENTION),
    _b("mention_down", ("down",), "↑↓", _MENTION),
    _b("mention_accept", ("enter", "tab"), "enter/tab", _MENTION),
    _b("mention_close", ("escape",), "esc", _MENTION),
    _b("lane_up", ("up",), "↑↓", _LANES),
    _b("lane_down", ("down",), "↑↓", _LANES),
    _b("focus_lane", ("enter",), "enter", _LANES),
    _b("rewind_prev", ("left",), "‹ ›", _REWIND),
    _b("rewind_next", ("right",), "‹ ›", _REWIND),
    _b("rewind_fork", ("enter",), "enter fork", _REWIND),
    _b("sessions_up", ("up",), "↑↓ select", _SESSIONS),
    _b("sessions_down", ("down",), "↑↓ select", _SESSIONS),
    _b("sessions_activate", ("enter",), "enter open", _SESSIONS),
    _b("evidence_prev", ("left",), "←/→", _EVIDENCE),
    _b("evidence_next", ("right",), "←/→", _EVIDENCE),
    _b("evidence_expand", ("enter",), "enter", _EVIDENCE),
    # Side-panel toggle (D7 AC4): opens/refreshes/closes the evidence
    # detail panel for the currently-selected claim; documented in the
    # block's own header hint (transcript_render._render_evidence),
    # mirroring how the header already advertises the other evidence
    # chords (single source: KEYMAP feeds both the bindings AND the hint).
    _b("evidence_detail", ("d",), "d detail", _EVIDENCE),
    # Approval bar (owns the keyboard while open, spec §7). Mockup
    # keydown: ``e.key === "Tab"`` matches with or without shift, so
    # shift+tab cycles the selection here — never the mode.
    _b("approval_prev", ("left", "up"), "arrows", _APPROVAL),
    _b("approval_next", ("right", "down", "tab", "shift+tab"), "arrows", _APPROVAL),
    _b("approval_confirm", ("enter",), "enter", _APPROVAL),
    # ctrl-y parks the live ticket into the needs-you queue without
    # answering it (ADR-0007 approvals: "ctrl-y defers head to
    # NeedsYouQueue"; the bar owns the keyboard, so the global ctrl-y
    # show_needs_you is suppressed while it is open). Handled by
    # ApprovalBar.on_key — documented here so the table stays the single
    # source of every approval-context chord (footer hint stays spec-exact).
    _b("approval_defer", ("ctrl+y",), "ctrl-y defer", _APPROVAL),
    # Esc chain — one binding per context; the app resolves priority via
    # ESC_CHAIN, never ad-hoc if/else ladders (spec §5).
    _b("lane_unfocus", ("escape",), "esc", _LANE_FOCUS),
    _b("close_palette", ("escape",), "esc", _PALETTE),
    _b("close_rewind", ("escape",), "esc", _REWIND),
    _b("close_sessions", ("escape",), "esc", _SESSIONS),
    _b("close_lanes", ("escape",), "esc", _LANES),
    _b("close_evidence", ("escape",), "esc", _EVIDENCE),
    _b("approval_deny", ("escape",), "esc", _APPROVAL),
    _b("interrupt_running", ("escape",), "esc", _RUNNING),
    # Display-only affordance: "/" is ordinary composer text that opens
    # the palette; the footer still advertises it.
    _b("open_palette", (), "/", frozenset()),
)


ESC_CHAIN: tuple[tuple[Context, str], ...] = (
    ("lane_focus", "lane_unfocus"),
    ("palette", "close_palette"),
    ("rewind", "close_rewind"),
    ("sessions", "close_sessions"),
    ("lanes", "close_lanes"),
    ("running", "interrupt_running"),
)
"""Esc priority order (DESIGN-SPEC §5, extended by S2 for the sessions
picker): the first entry whose context is active consumes the Esc press.
``sessions`` sits right after ``rewind`` -- both are single-purpose picker
strips opened by an explicit command, so they share precedence ahead of
the more ambient ``lanes`` panel. (Approval and evidence esc handling are
context-exclusive — the approval bar owns the keyboard, and evidence esc
only fires while the evidence block has focus — so they sit outside the
global chain.)"""

ESC_BACKTRACK_WINDOW_SECONDS = 0.75
"""A second Esc after interrupt opens rewind through the existing picker."""


# Footer hint strings — EXACT text per DESIGN-SPEC §2.
FOOTER_HINTS: dict[str, str] = {
    "approval": "arrows select · enter confirm · esc deny",
    "lane_focus": "esc back to parent · transcript is the subagent's own",
    "palette": "↑↓ select · enter run · esc close",
    "mention": "↑↓ select · enter/tab insert · esc close",
    "sessions": "↑↓ select · enter open · esc close",
    "running": "esc interrupt · enter steer · shift+enter queue",
    "idle": "",
}
"""Compliance 2026-08-02, item D4 (David Koleczek's UX review, July 31):
``idle`` used to carry a generic, always-on reminder (history/newline/
rewind/commands) that occupied the footer's right segment on literally
every frame the composer wasn't running or overlaid -- the majority of a
session. That text never changed, so it wasn't a *hint* so much as
permanent teaching copy squatting on status real estate (AC2/AC3: the
footer reserves its space for transient status, attention, and the
actions actually available *right now*). The literal table value here
stays empty -- not replaced with a shorter reminder -- because the same
shortcuts are taught progressively elsewhere: :data:`COMPOSER_PLACEHOLDER`
(shown exactly when there is empty space to teach in) and the ``/keys``
command (:func:`help_rows`, reachable any time via the palette). The
other entries stay: they are genuinely context-sensitive -- tied to a
live overlay or a running turn, not shown "every frame".

Post-merge audit (Finding 1): D4 deliberately dropped the ``ctrl-r
rewind`` fragment along with the rest of that generic reminder, but item
S1 (already DONE before this merge train) has its own AC1 requiring the
footer to expose the rewind shortcut "in plain language when the action
is available" -- which the table's bare "" no longer did, regardless of
whether checkpoints existed. :func:`~amplifier_app_tui.ui.footer.
footer_right_text` reconciles both constraints by composing ``idle``'s
hint live (the same shape ``running`` already uses for its own dynamic
queue-chord swap) instead of a static table lookup: still exactly this
"" the table defines whenever rewind is not genuinely available, but
``ctrl-r rewind`` -- and only that one chord, never the rest of the old
row -- the moment a checkpoint exists (see ``FooterState.
rewind_available`` and ``test_ui_footer.py``). AC2/AC3 hold exactly as
before: nothing rides every frame, only a state-tied, immediately-
available action does.
"""


COMPOSER_PLACEHOLDER = (
    "Message Amplifier…  ( ↑ history · ctrl+j newline · enter send · / commands )"
)
"""Composer placeholder — exact string per DESIGN-SPEC §2."""


HELP_ACTIONS: tuple[str, ...] = (
    "submit",
    "insert_newline",
    "history_prev",
    "queue_message",
    "cycle_mode",
    "cycle_permission",
    "cycle_effort",
    "toggle_lanes",
    "cycle_tail",
    "open_external_editor",
    "toggle_thinking",
    "show_ledger",
    "show_needs_you",
    "open_rewind",
    "return_to_answer",
    "plan_drilldown",
    "toggle_plan_overflow",
    "stash_prompt",
    "open_palette",
)
"""Actions worth teaching once, in ``/keys`` listing order (item D4).

These are the "anytime" chords -- stable across a session, not tied to one
overlay -- that used to ride the footer's generic ``idle`` hint. Chords
that are only meaningful while a specific overlay owns the keyboard
(palette/mention/lanes/rewind/approval/evidence navigation) are left out
on purpose: those stay taught live by that overlay's own
:data:`FOOTER_HINTS` entry the moment it's relevant, which is the more
honest "context-sensitive prompt" (AC2) than repeating them in a static
reference.
"""

ACTION_HELP: dict[str, str] = {
    "submit": "send your message (steers the current turn instead, if one is running)",
    "insert_newline": "add a newline without sending",
    "history_prev": "recall an earlier prompt (↓ for newer / your current draft)",
    "queue_message": "queue a full next turn while one runs (alt+enter on legacy terminals)",
    "cycle_mode": "cycle posture: chat → plan → brainstorm → build → auto",
    "cycle_permission": "show the current trust posture",
    "cycle_effort": "cycle reasoning-effort tier (none…xhigh)",
    "toggle_lanes": "toggle the agent lanes panel",
    "cycle_tail": "cycle live-tail focus while agents run",
    "open_external_editor": "compose the draft in $VISUAL/$EDITOR",
    "toggle_thinking": "show/hide the live thinking box while a turn runs",
    "show_ledger": "print the session outcome ledger",
    "show_needs_you": "open deferred decisions",
    "open_rewind": "open the rewind picker to fork an earlier turn (forking mid-turn interrupts the turn first)",
    "return_to_answer": "jump back to the current/most-recent turn's final answer",
    "plan_drilldown": "cycle the plan panel's row window",
    "toggle_plan_overflow": "expand or collapse the plan panel's hidden rows",
    "stash_prompt": "stash the in-progress draft; /unstash restores it",
    "open_palette": "open the command palette",
}
"""One-line descriptions for :func:`help_rows`, keyed by keymap action id.

Keying by action (not by literal key text) means the text can never drift
from the bound chord -- a keymap edit changes the label :func:`hint_label`
returns and this description rides along unchanged (DEVELOPMENT.md:
"Keymap is data").
"""


def help_rows(actions: tuple[str, ...] = HELP_ACTIONS) -> tuple[tuple[str, str], ...]:
    """``(label, description)`` pairs for the ``/keys`` reference, in order.

    Labels are read live from :func:`hint_label` -- the same lookup the
    footer hints use -- so this listing is rendered FROM the keymap table,
    never hand-copied alongside it (single shared source, item D4: "move
    shortcut definitions into a shared keymap/help source so removing
    hints does not reduce discoverability").
    """
    return tuple((hint_label(action), ACTION_HELP[action]) for action in actions)


def validate(keymap: tuple[Binding, ...] = KEYMAP) -> None:
    """Reject malformed tables.

    Fails on: empty actions, oversized or missing labels, and — the point
    of the exercise — two different actions claiming the same key while
    the same context is active. Alternate chords for the SAME action
    (shift+enter / alt+enter) are allowed.
    """
    claimed: dict[tuple[str, Context], str] = {}
    for binding in keymap:
        if not binding.action:
            raise ValueError("binding with empty action")
        if not binding.label:
            raise ValueError(f"binding {binding.action!r} needs a display label")
        if len(binding.label) > _MAX_LABEL_CHARS:
            raise ValueError(f"binding {binding.action!r} display label too long")
        for key in binding.keys:
            for context in binding.contexts:
                slot = (key, context)
                other = claimed.get(slot)
                if other is not None and other != binding.action:
                    raise ValueError(
                        f"key {key!r} in context {context!r} is claimed by both "
                        f"{other!r} and {binding.action!r}"
                    )
                claimed[slot] = binding.action


def _build_hint_labels(keymap: tuple[Binding, ...]) -> dict[str, str]:
    """Action → first labeled non-fallback binding (fallbacks never win
    the advertised label by default)."""
    labels: dict[str, str] = {}
    for binding in keymap:
        if binding.label and not binding.fallback and binding.action not in labels:
            labels[binding.action] = binding.label
    for binding in keymap:  # fallback-only actions still get a label
        if binding.label and binding.action not in labels:
            labels[binding.action] = binding.label
    return labels


_HINT_LABELS = _build_hint_labels(KEYMAP)


def hint_label(action: str, overrides: Mapping[str, str] | None = None) -> str:
    """On-screen label for *action* (first labeled table entry wins).

    ``overrides`` is the terminal-capability seam: after the probe, pass
    ``{"queue_message": "alt+enter"}`` on terminals where real
    shift+enter never arrives. Raises ``KeyError`` for unknown actions so
    a typo fails loudly instead of rendering a stale shortcut.
    """
    if overrides is not None:
        override = overrides.get(action)
        if override:
            return override[:_MAX_LABEL_CHARS]
    try:
        return _HINT_LABELS[action]
    except KeyError:
        raise KeyError(f"no display label for action {action!r}") from None


def bindings_for(context: Context, keymap: tuple[Binding, ...] = KEYMAP) -> tuple[Binding, ...]:
    """All bindings active in *context*, in table order."""
    return tuple(b for b in keymap if context in b.contexts)


__all__ = [
    "ALL_CONTEXTS",
    "Binding",
    "COMPOSER_PLACEHOLDER",
    "Context",
    "ESC_CHAIN",
    "ESC_BACKTRACK_WINDOW_SECONDS",
    "FOOTER_HINTS",
    "KEYMAP",
    "NO_APPROVAL",
    "bindings_for",
    "hint_label",
    "validate",
]
