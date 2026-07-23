"""The attention-notification ladder: bell -> OSC 777 desktop -> push.

The shipped bell (``App.bell``) is the one escape path Textual proves safe
and it works everywhere, but it is easy to miss when the terminal window is
unfocused. This module adds the next rung: an OSC 777 desktop notification
written through the same sanctioned ``driver.write`` path the native
terminal title already uses (``ui/chrome.write_terminal_title``) -- an
out-of-band escape the terminal renders as a real OS notification, so it
never touches the Textual screen grid. The third rung, off-machine push, is
owned by the mounted ``hooks-notify-push`` module (ntfy), so it lives
outside the app kernel entirely.

Everything here is a pure function of its inputs (no Textual, no
amplifier-core): escape-sequence builders, terminal-support detection, and
the ladder policy. ``ui/app.py`` supplies the live driver, focus state, and
environment and performs the single side effect (the write).

Donor parity (amplifier-app-cli, read-only reference): the OSC 777
``\x1b]777;notify;<title>;<body>\x07`` shape and 80/240-char bounds mirror
``ui/repl.terminal_notification_sequence``; the terminal allowlist and the
``AMPLIFIER_TERMINAL_NOTIFICATIONS`` off/force override mirror
``ui/terminal_probe.osc9_notifications_supported``; the notify-only-when-
unfocused trigger mirrors ``ui/layered_repl_terminal.notify_turn_complete``.
Re-expressed through NewTUI's own seams -- nothing is imported or vendored.
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from textual.driver import Driver

Reason = Literal["turn_finished", "decision_deferred"]
"""Why attention is being requested (mirrors ``attention_bell_needed``)."""

Rung = Literal["bell", "desktop"]
"""A step on the notification ladder the app knows how to fire itself.

``bell`` is Textual's driver-safe ``App.bell``; ``desktop`` is the OSC 777
sequence written to the terminal. Off-machine ``push`` is the mounted
``hooks-notify-push`` module's job and never appears here.
"""

NotifyCeiling = Literal["off", "bell", "desktop"]
"""How high ``AMPLIFIER_NOTIFY`` lets the ladder climb (parsed value)."""

ATTENTION_MIN_TURN_SECONDS = 10.0
"""Turn-end threshold: a turn shorter than this is a live exchange (the
user is watching); a longer one plausibly lost their attention, so its
close-out notifies. Deferred decisions always notify -- they block on the
human by definition."""

_NOTIFY_DISABLED_VALUES = frozenset({"false", "0", "no", "off"})
"""``AMPLIFIER_NOTIFY`` values that silence every rung -- the exact kill
switch the (suppressed) hooks-notify module honored, kept for parity."""

_NOTIFY_BELL_ONLY_VALUES = frozenset({"bell"})
"""``AMPLIFIER_NOTIFY`` values that cap the ladder at the audible bell and
never climb to a desktop notification."""

# -- terminal-support allowlist (donor: osc9_notifications_supported) --------

NOTIFY_TERMINAL_ENV = "AMPLIFIER_TERMINAL_NOTIFICATIONS"
"""Escape hatch for desktop notifications: ``off`` silences them on
allowlisted terminals; ``force`` enables them anywhere."""

_TERMINAL_OFF_VALUES = frozenset({"off", "0", "false", "never", "none"})
_TERMINAL_FORCE_VALUES = frozenset({"force", "on", "1", "true", "always"})
_OSC_NOTIFY_TERM_PROGRAMS = frozenset({"ghostty", "iterm.app", "wezterm", "warpterminal"})
"""``TERM_PROGRAM`` values (lowercased) of terminals known to render OSC
notifications. Other terminals may print the escape as garbage, so they are
excluded unless ``AMPLIFIER_TERMINAL_NOTIFICATIONS=force`` opts them in."""

# -- OSC 777 escape sequence (donor: terminal_notification_sequence) ---------

_MAX_TITLE_CHARS = 80
_MAX_BODY_CHARS = 240


def notify_ceiling(environ: Mapping[str, str] | None = None) -> NotifyCeiling:
    """Parse ``AMPLIFIER_NOTIFY`` into the highest rung the ladder may use.

    ``false``/``0``/``no``/``off`` -> ``off`` (silence, the historical kill
    switch); ``bell`` -> ``bell`` (audible only, never desktop); anything
    else -- unset, ``true``/``1``/``on``, or an explicit ``desktop`` -- opens
    the full ladder. Unknown values default to the full ladder so a typo
    never silences you.
    """
    env = os.environ if environ is None else environ
    value = env.get("AMPLIFIER_NOTIFY", "").strip().lower()
    if value in _NOTIFY_DISABLED_VALUES:
        return "off"
    if value in _NOTIFY_BELL_ONLY_VALUES:
        return "bell"
    return "desktop"


def attention_needed(
    reason: Reason,
    elapsed_s: float = 0.0,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Whether any rung should fire for *reason*.

    Deferred decisions always qualify; a finished turn qualifies only once
    it has run past :data:`ATTENTION_MIN_TURN_SECONDS`. ``AMPLIFIER_NOTIFY``
    set to a disabled value suppresses everything.
    """
    if notify_ceiling(environ) == "off":
        return False
    if reason == "decision_deferred":
        return True
    return elapsed_s >= ATTENTION_MIN_TURN_SECONDS


def desktop_notifications_supported(
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Allowlist OSC 777 desktop notifications by terminal identity.

    ghostty, iTerm2, WezTerm and Warp (via ``TERM_PROGRAM``) and kitty (via
    ``TERM``/``KITTY_WINDOW_ID``) render OSC notifications; other terminals
    may print the raw escape, so they are excluded.
    ``AMPLIFIER_TERMINAL_NOTIFICATIONS=off`` silences them anywhere and
    ``=force`` enables them anywhere.
    """
    env = os.environ if environ is None else environ
    override = env.get(NOTIFY_TERMINAL_ENV, "").strip().lower()
    if override in _TERMINAL_OFF_VALUES:
        return False
    if override in _TERMINAL_FORCE_VALUES:
        return True
    if env.get("TERM_PROGRAM", "").strip().lower() in _OSC_NOTIFY_TERM_PROGRAMS:
        return True
    return "kitty" in env.get("TERM", "") or bool(env.get("KITTY_WINDOW_ID"))


def notification_rungs(
    reason: Reason,
    elapsed_s: float = 0.0,
    *,
    focused: bool = True,
    environ: Mapping[str, str] | None = None,
) -> tuple[Rung, ...]:
    """The ordered rungs to fire for *reason* -- the ladder decision.

    Nothing fires unless attention is actually needed (:func:`attention_
    needed`). The audible bell is always the first rung. The ladder climbs
    to the OSC 777 desktop rung only when the escalation is warranted and
    permitted: the terminal window is **unfocused** (the user looked away,
    exactly when a desktop toast earns its keep), the terminal is on the
    render allowlist, and ``AMPLIFIER_NOTIFY`` was not capped at ``bell``.
    """
    if not attention_needed(reason, elapsed_s, environ=environ):
        return ()
    rungs: list[Rung] = ["bell"]
    if (
        notify_ceiling(environ) == "desktop"
        and not focused
        and desktop_notifications_supported(environ)
    ):
        rungs.append("desktop")
    return tuple(rungs)


def sanitize_notification_text(text: str) -> str:
    """Collapse untrusted text into one safe, control-free display line.

    Control characters (including a smuggled ``ESC``/``BEL`` that could end
    the OSC early and inject a second sequence) become spaces; bidi and
    other invisible formatting codepoints are dropped; whitespace runs
    collapse to single spaces. The caller bounds the length per field.
    """
    kept: list[str] = []
    for character in str(text):
        category = unicodedata.category(character)
        if category == "Cc":  # C0/C1 controls (ESC, BEL, \n, \t) -> space
            kept.append(" ")
        elif category == "Cf":  # bidi / zero-width / invisible formatters -> drop
            continue
        else:
            kept.append(character)
    return " ".join("".join(kept).split())


def osc777_notification_sequence(title: str, body: str) -> str:
    """Build a bounded OSC 777 notification with escape injection stripped.

    Shape ``\x1b]777;notify;<title>;<body>\x07`` (BEL-terminated) -- the
    kitty/wezterm/rxvt desktop-notification form, rendered as a native OS
    toast. Title and body are sanitized and capped (80/240 chars) so a
    verbose recap cannot flood the notification or break out of the OSC.
    """
    safe_title = sanitize_notification_text(title)[:_MAX_TITLE_CHARS].rstrip()
    safe_body = sanitize_notification_text(body)[:_MAX_BODY_CHARS].rstrip()
    return f"\x1b]777;notify;{safe_title};{safe_body}\x07"


def write_desktop_notification(driver: Driver | None, title: str, body: str) -> bool:
    """Emit an OSC 777 desktop notification through the Textual driver.

    Mirrors ``chrome.write_terminal_title``: the escape is written on the
    driver's own synchronized output stream (never raw ``stdout``, which
    would race the compositor), and skipped when there is no real terminal
    to receive it. Returns whether the sequence was written; never raises.
    """
    if driver is None or driver.is_headless or driver.is_web:
        return False
    driver.write(osc777_notification_sequence(title, body))
    driver.flush()
    return True


__all__ = [
    "ATTENTION_MIN_TURN_SECONDS",
    "NOTIFY_TERMINAL_ENV",
    "NotifyCeiling",
    "Reason",
    "Rung",
    "attention_needed",
    "desktop_notifications_supported",
    "notification_rungs",
    "notify_ceiling",
    "osc777_notification_sequence",
    "sanitize_notification_text",
    "write_desktop_notification",
]
