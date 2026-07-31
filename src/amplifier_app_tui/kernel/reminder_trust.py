"""Trust classification for injected ``<system-reminder>`` context blocks.

Hooks inject ephemeral guidance into the model's context wrapped in
``<system-reminder source="...">`` tags (mode manuals, git/status context,
todo reminders, routing matrices). These are *context for the model*, never
turns the human authored — so they must never be replayed into the user's
transcript as if the user (or the model) had said them.

The chokepoint that keeps injected reminders out of the transcript used to
test ``text.startswith("<system-reminder>")`` — an exact, attribute-free
prefix. Every reminder a real hook emits carries a ``source="..."``
attribute (``<system-reminder source="hooks-status-context">`` …), so that
prefix matched *none* of them and let attributed injections replay as fake
user turns. This module makes the classification attribute-tolerant and
gives it one pure, tested home.

It also names the second half of the trust boundary: some housekeeping
reminders (status-context, todo-reminder) legitimately instruct the model to
"process silently" and "do not mention this to the user." That convention is
benign, but under a no-tools *denial* — where the tools that would justify it
are stripped — the same directives read as an adversarial prompt injection.
tui never honors such a directive to suppress user-facing output;
:func:`has_concealment_directive` lets the resume path *log* (never silence)
when it drops one, so the trust event is observable rather than swallowed.
"""

from __future__ import annotations

import re

_REMINDER_OPEN = re.compile(r"^<system-reminder(?:\s[^>]*)?>", re.IGNORECASE)
"""Opening ``<system-reminder>`` tag, with or without attributes."""

_SOURCE_ATTR = re.compile(r'source\s*=\s*"([^"]*)"', re.IGNORECASE)
"""The ``source="..."`` provenance attribute on a reminder open tag."""

_CONCEALMENT_PATTERNS = (
    re.compile(r"\bnever mention this\b", re.IGNORECASE),
    re.compile(r"do not (?:mention|tell|reveal|surface)[^.\n]{0,60}\buser\b", re.IGNORECASE),
    re.compile(r"\bwithout (?:telling|informing|notifying)[^.\n]{0,30}\buser\b", re.IGNORECASE),
    re.compile(r"\bprocess (?:this )?silently\b", re.IGNORECASE),
)
"""'Hide this from the user' phrasings seen in real housekeeping reminders."""


def is_injected_reminder(text: str) -> bool:
    """True if *text* is an injected ``<system-reminder>`` context block.

    Attribute-tolerant: matches both the bare ``<system-reminder>`` form and
    the attributed ``<system-reminder source="hooks-status-context">`` form
    every real hook actually emits.
    """
    return bool(_REMINDER_OPEN.match(text.strip()))


def reminder_source(text: str) -> str | None:
    """The ``source="..."`` provenance of an injected reminder, or ``None``.

    Only reads the attribute on the opening tag — ``source=`` occurring later
    in the body does not count.
    """
    stripped = text.strip()
    open_match = _REMINDER_OPEN.match(stripped)
    if open_match is None:
        return None
    source_match = _SOURCE_ATTR.search(stripped[: open_match.end()])
    return source_match.group(1) if source_match else None


def has_concealment_directive(text: str) -> bool:
    """True if *text* instructs the model to hide something from the user.

    Detects the "never mention this reminder to the user" / "process
    silently" convention that status-context and todo-reminder hooks inject.
    Benign in intent, but never a licence for tui to suppress
    user-facing output — this predicate exists to *surface* such directives,
    not to obey them.
    """
    return any(pattern.search(text) for pattern in _CONCEALMENT_PATTERNS)


__all__ = [
    "has_concealment_directive",
    "is_injected_reminder",
    "reminder_source",
]
