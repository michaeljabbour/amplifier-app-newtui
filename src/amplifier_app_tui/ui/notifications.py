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
Re-expressed through TUI's own seams -- nothing is imported or vendored.

-- The attention-event contract (B7, issue #47) -----------------------------

:class:`AttentionRecord` is the ONE normalized shape every destination
(bell, OSC 777 desktop, and -- conceptually -- ntfy push) consumes instead
of each destination deriving its own notion of "does this need to fire" from
ad-hoc call sites. :class:`AttentionCenter` mints exactly one record per
transition into an attention state and deduplicates repeats (a re-render, a
reconnect, a repeated kernel-side ping for the same already-parked decision)
by a stable idempotency key (``event_id``) rather than by wall-clock time or
message text. This is the seam item B8 (voice-first/ambient delegation)
builds on: a session's current attention state -- and its acknowledgement --
is queryable independent of which local destinations happen to be wired up.

Honest scope note on the ladder's third rung: off-machine ntfy push is
fired by the separately-mounted ``hooks-notify-push`` module listening to
the raw kernel ``orchestrator:complete`` event (bundle.md) -- a different
process/device boundary this module does not reach into. It is not, and
cannot be, driven through :class:`AttentionCenter` or acknowledged/cleared
from here; :func:`clear_desktop_notification` only ever addresses the
in-terminal OSC 777 rung this process itself wrote.
"""

from __future__ import annotations

import logging
import os
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from textual.driver import Driver

logger = logging.getLogger(__name__)

AttentionReason = Literal[
    "completion",
    "awaiting_approval",
    "awaiting_clarification",
    "error",
]
"""Why attention is being requested -- the four states the audited design
notes call out: a turn reached a successful close-out, a governance/tool
decision is parked awaiting the human's approval, a question-tool style ask
is parked awaiting clarification, or the session hit an error state."""

_ALWAYS_QUALIFIES: frozenset[AttentionReason] = frozenset(
    {"awaiting_approval", "awaiting_clarification", "error"}
)
"""Reasons that always need attention regardless of elapsed time -- they
block on the human by definition (a parked decision or an error), mirroring
the historical ``decision_deferred`` rule."""

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
close-out notifies. Reasons that always qualify (awaiting approval/
clarification, error) notify regardless -- they block on the human by
definition."""

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
    reason: AttentionReason,
    elapsed_s: float = 0.0,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Whether any rung should fire for *reason*.

    Awaiting-approval, awaiting-clarification and error states always
    qualify; a completion qualifies only once it has run past
    :data:`ATTENTION_MIN_TURN_SECONDS`. ``AMPLIFIER_NOTIFY`` set to a
    disabled value suppresses everything -- including :class:`AttentionRecord`
    creation (:meth:`AttentionCenter.note` is only ever called when this is
    true), so a fully-muted session mints no records either, matching
    today's behavior byte-for-byte when notifications are off.
    """
    if notify_ceiling(environ) == "off":
        return False
    if reason in _ALWAYS_QUALIFIES:
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
    reason: AttentionReason,
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
    to receive it. Returns whether the sequence was written; never raises --
    a destination failure is logged and swallowed, never allowed to block
    the session (WHAT TO BUILD #5).
    """
    if driver is None or driver.is_headless or driver.is_web:
        return False
    try:
        driver.write(osc777_notification_sequence(title, body))
        driver.flush()
    except Exception:  # noqa: BLE001 -- a destination failure must never block the session
        logger.warning("desktop notification write failed", exc_info=True)
        return False
    return True


def clear_desktop_notification(driver: Driver | None) -> bool:
    """Best-effort clear of the OSC 777 desktop rung's indicator (AC5).

    The escape is ours to rewrite: terminals/multiplexers that keep a
    persistent tab or window indicator keyed to the last OSC 777 payload
    retire it on an empty follow-up notification. This is explicitly
    best-effort, not a guarantee -- a terminal that already rendered a
    one-shot OS toast banner cannot have that banner recalled by any escape
    sequence, honestly, there is nothing to retract. Safe to call even when
    nothing was ever shown (an empty write is inert). Off-machine ntfy push
    is a separate destination this process does not own and has no
    acknowledgement channel here at all (see module docstring) -- this
    function never touches it. Never raises.
    """
    return write_desktop_notification(driver, "", "")


def fire_attention_ladder(
    rungs: tuple[Rung, ...],
    *,
    bell: Callable[[], None],
    driver: Driver | None,
    title: str,
    body: str,
) -> tuple[Rung, ...]:
    """Fire each of *rungs*, containing (logging, never raising) failures.

    A destination problem -- the bell call raising, the driver write
    failing -- must never block the session (WHAT TO BUILD #5): each rung
    is attempted independently, a failure is logged with the offending rung
    named, and the remaining rungs still fire. Returns the rungs that fired
    without error.
    """
    fired: list[Rung] = []
    for rung in rungs:
        try:
            if rung == "bell":
                bell()
            else:
                write_desktop_notification(driver, title, body)
        except Exception:  # noqa: BLE001 -- a destination failure must never block the session
            logger.warning("attention destination %r failed", rung, exc_info=True)
            continue
        fired.append(rung)
    return tuple(fired)


# -- the normalized attention record (B7, issue #47) -------------------------


@dataclass(frozen=True, slots=True)
class AttentionRecord:
    """One normalized "the assistant needs you" event.

    The single shape every destination consumes instead of an ad-hoc call
    site (WHAT TO BUILD #1/#3). Minted exactly once per transition into an
    attention state by :meth:`AttentionCenter.note` -- never construct one
    directly outside this module, or the idempotency guarantee below no
    longer holds.

    Attributes:
        session_id: Whose session this belongs to (the runtime adapter's
            session id) -- multi-session/ambient-delegation aware (B8:
            distinguishes "session X needs you" from "session Y needs you").
        reason: One of the four attention states (see :data:`AttentionReason`).
        event_id: The stable idempotency key for THIS transition, deterministic
            in ``(session_id, reason, occasion)`` (see :func:`attention_event_id`).
            The same transition -- the same turn finishing, the same decision
            parked -- always mints the same id; a genuinely new transition
            (a new turn, a new decision) mints a new one. Destinations and
            callers dedupe by this id, never by wall-clock time or message
            text.
        detail: Human-readable one-line detail (the deferred-decision
            question, an error message, ...); destinations fold it into
            their body/message. Empty when the reason's default suffices.
        created_at: ``time.time()`` when the transition was recorded
            (telemetry/ordering only -- never part of the idempotency key).
        acknowledged: Whether the human has resolved this attention state
            (answered the decision, or the session/window resumed) --
            see :meth:`AttentionCenter.acknowledge`.
    """

    session_id: str
    reason: AttentionReason
    event_id: str
    detail: str = ""
    created_at: float = 0.0
    acknowledged: bool = False

    def acknowledge(self) -> AttentionRecord:
        """Return an acknowledged copy (records are immutable)."""
        return replace(self, acknowledged=True)


def attention_event_id(session_id: str, reason: AttentionReason, occasion: str) -> str:
    """The stable idempotency key for one attention transition.

    Deterministic in ``(session_id, reason, occasion)`` -- NOT a random uuid
    or a timestamp, which would defeat dedup by construction. ``occasion``
    is the caller's stable handle on the underlying thing that is asking
    for attention: a turn id for a completion, a decision id for an
    awaiting-approval/clarification. The same occasion recurring (a
    re-render, a reconnect, a repeated kernel-side ping for an
    already-parked decision) always reproduces the same id.
    """
    return f"{session_id or 'local'}:{reason}:{occasion}"


class AttentionCenter:
    """Owns the ladder's dedupe + acknowledgement bookkeeping (B7, issue #47).

    :meth:`note` is the ONLY place a transition into an attention state
    becomes an :class:`AttentionRecord` (AC1). Call it on every transition
    unconditionally -- a re-render or a reconnect that reports the exact
    same ``(session_id, reason, occasion)`` gets back the SAME record with
    ``is_new=False`` instead of a fresh one, so the caller's "did this just
    happen" check is a one-line ``if is_new``. This is what makes AC3
    (repeated rendering/reconnecting must not duplicate) hold structurally
    rather than by caller discipline.

    :meth:`acknowledge` clears the session's current (most recent) record
    when the human acts -- answers a decision, or the session/window
    resumes (AC5). "Current" is deliberately singular: destinations
    (bell/desktop) express a session-global "come look" signal, not a
    per-decision one, so the latest transition supersedes an older
    unacknowledged one as the thing actually demanding attention.

    Not thread-safe by itself -- like every other :class:`~amplifier_app_tui.
    ui.reducer.ReducerHost` mutation, it is only ever touched from the UI
    thread; the real runtime's cross-thread events are already marshalled
    onto that thread before they reach here. Memory is bounded by process
    lifetime (every event id ever minted is retained, never evicted) -- a
    documented limitation, not a leak: a TUI session's turn/decision count
    is small enough that this never matters in practice.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, AttentionRecord] = {}
        self._current: dict[str, str] = {}  # session_id -> its latest event_id

    def note(
        self,
        session_id: str,
        reason: AttentionReason,
        occasion: str,
        *,
        detail: str = "",
        now: float | None = None,
    ) -> tuple[AttentionRecord, bool]:
        """Record a transition into an attention state.

        Returns ``(record, is_new)``. ``is_new`` is ``False`` when this
        exact transition was already recorded -- the caller should only
        fire the destination ladder when it is ``True``.
        """
        event_id = attention_event_id(session_id, reason, occasion)
        existing = self._by_id.get(event_id)
        if existing is not None:
            return existing, False
        record = AttentionRecord(
            session_id=session_id,
            reason=reason,
            event_id=event_id,
            detail=detail,
            created_at=time.time() if now is None else now,
        )
        self._by_id[event_id] = record
        self._current[session_id] = event_id
        return record, True

    def acknowledge(self, session_id: str) -> AttentionRecord | None:
        """Clear the current attention record for *session_id*.

        Returns the newly-acknowledged record (the caller uses this to
        decide whether to clear destination indicators), or ``None`` when
        there was nothing open -- an idle acknowledge (e.g. a plain window
        refocus with no pending attention) is a deliberate no-op, not an
        error.
        """
        event_id = self._current.get(session_id)
        if event_id is None:
            return None
        record = self._by_id[event_id]
        if record.acknowledged:
            return None
        acked = record.acknowledge()
        self._by_id[event_id] = acked
        return acked

    def current(self, session_id: str) -> AttentionRecord | None:
        """The most recently minted record for *session_id*, if any."""
        event_id = self._current.get(session_id)
        return None if event_id is None else self._by_id.get(event_id)


__all__ = [
    "ATTENTION_MIN_TURN_SECONDS",
    "NOTIFY_TERMINAL_ENV",
    "AttentionCenter",
    "AttentionReason",
    "AttentionRecord",
    "NotifyCeiling",
    "Rung",
    "attention_event_id",
    "attention_needed",
    "clear_desktop_notification",
    "desktop_notifications_supported",
    "fire_attention_ladder",
    "notification_rungs",
    "notify_ceiling",
    "osc777_notification_sequence",
    "sanitize_notification_text",
    "write_desktop_notification",
]
