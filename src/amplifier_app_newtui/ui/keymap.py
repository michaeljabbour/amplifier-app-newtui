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
    "lanes",  # agent lanes panel open
    "lane_focus",  # a subagent lane is focused (child transcript shown)
    "rewind",  # rewind picker strip open
    "approval",  # approval bar replaces the composer
    "needs_you",  # needs-you block focused
    "evidence",  # evidence block open
]

ALL_CONTEXTS: frozenset[Context] = frozenset(
    (
        "idle",
        "running",
        "palette",
        "lanes",
        "lane_focus",
        "rewind",
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
_LANES: frozenset[Context] = frozenset({"lanes"})
_LANE_FOCUS: frozenset[Context] = frozenset({"lane_focus"})
_REWIND: frozenset[Context] = frozenset({"rewind"})
_APPROVAL: frozenset[Context] = frozenset({"approval"})
_EVIDENCE: frozenset[Context] = frozenset({"evidence"})
_RUNNING: frozenset[Context] = frozenset({"running"})
_IDLE: frozenset[Context] = frozenset({"idle"})

KEYMAP: tuple[Binding, ...] = (
    # Submission / steering / queueing (spec §5).
    _b("submit", ("enter",), "enter", _IDLE),
    _b("steer", ("enter",), "enter", _RUNNING),
    _b("queue_message", ("shift+enter",), "shift+enter", NO_APPROVAL),
    _b("queue_message", ("alt+enter",), "alt+enter", NO_APPROVAL, fallback=True),
    # Mode & permission cycles (independent controls, ADR-0005 amendment).
    _b("cycle_mode", ("shift+tab",), "shift+tab", NO_APPROVAL),
    _b("cycle_permission", ("ctrl+p",), "ctrl-p", NO_APPROVAL),
    # Panels / pickers.
    _b("toggle_lanes", ("ctrl+t",), "ctrl-t", NO_APPROVAL),
    _b("show_ledger", ("ctrl+l",), "ctrl-l", NO_APPROVAL),
    _b("show_needs_you", ("ctrl+y",), "ctrl-y", NO_APPROVAL),
    _b("open_rewind", ("ctrl+r",), "ctrl-r", NO_APPROVAL),
    # In-panel navigation.
    _b("palette_up", ("up",), "↑↓", _PALETTE),
    _b("palette_down", ("down",), "↑↓", _PALETTE),
    _b("palette_run", ("enter",), "enter", _PALETTE),
    _b("lane_up", ("up",), "↑↓", _LANES),
    _b("lane_down", ("down",), "↑↓", _LANES),
    _b("focus_lane", ("enter",), "enter", _LANES),
    _b("rewind_prev", ("left",), "‹ ›", _REWIND),
    _b("rewind_next", ("right",), "‹ ›", _REWIND),
    _b("rewind_fork", ("enter",), "enter fork", _REWIND),
    _b("evidence_prev", ("left",), "←/→", _EVIDENCE),
    _b("evidence_next", ("right",), "←/→", _EVIDENCE),
    _b("evidence_expand", ("enter",), "enter", _EVIDENCE),
    # Approval bar (owns the keyboard while open, spec §7). Mockup
    # keydown: ``e.key === "Tab"`` matches with or without shift, so
    # shift+tab cycles the selection here — never the mode.
    _b("approval_prev", ("left", "up"), "arrows", _APPROVAL),
    _b("approval_next", ("right", "down", "tab", "shift+tab"), "arrows", _APPROVAL),
    _b("approval_confirm", ("enter",), "enter", _APPROVAL),
    # Esc chain — one binding per context; the app resolves priority via
    # ESC_CHAIN, never ad-hoc if/else ladders (spec §5).
    _b("lane_unfocus", ("escape",), "esc", _LANE_FOCUS),
    _b("close_palette", ("escape",), "esc", _PALETTE),
    _b("close_rewind", ("escape",), "esc", _REWIND),
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
    ("lanes", "close_lanes"),
    ("running", "interrupt_running"),
)
"""Esc priority order (DESIGN-SPEC §5): the first entry whose context is
active consumes the Esc press. (Approval and evidence esc handling are
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
    "running": "esc interrupt · enter steer · shift+enter queue",
    "idle": "/ commands · shift+tab mode · ctrl-t tasks",
}


COMPOSER_PLACEHOLDER = (
    "Message Amplifier…  "
    "( / commands · shift+tab mode · enter send · type mid-turn to steer )"
)
"""Composer placeholder — exact string per DESIGN-SPEC §2."""


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
