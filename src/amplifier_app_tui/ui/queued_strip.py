"""Queued-message overlay strip (DESIGN-SPEC §2/§5).

A one-line orange strip docked ABOVE the composer, shown while a full
next-turn message is queued (Shift+Enter while running, or a second
steer):

``▹ queued next: "<text>" · runs when this turn ends · alt+↑ recall to steer``

The SteeringQueue owns the state and the footer shows the ``· q1`` badge.
Clicking the strip (or pressing Alt+Up) recalls the exact text into an empty
composer, where Enter can steer the current turn without retyping it.
"""

from __future__ import annotations

from rich.text import Text
from textual import events
from textual.message import Message
from textual.widgets import Static

from ..model.blocks import GLYPH_QUEUED

RECALL_HINT = "alt+↑ recall to steer"
QUEUED_PREVIEW_CHARS = 120


def _queued_preview(text: str) -> str:
    """One bounded line for the strip; the queue retains the full payload."""
    compact = " ".join(text.split())
    if len(compact) <= QUEUED_PREVIEW_CHARS:
        return compact
    return compact[: QUEUED_PREVIEW_CHARS - 1].rstrip() + "…"


def queued_text(text: str) -> str:
    """Queued message plus the visible, keyboard-reachable recall action."""
    preview = _queued_preview(text)
    return f'{GLYPH_QUEUED} queued next: "{preview}" · runs when this turn ends · {RECALL_HINT}'


class QueuedStrip(Static):
    """The queued-next-message strip (orange, bordered, above composer)."""

    class RecallRequested(Message):
        """Click requested the same recall action as Alt+Up."""

    DEFAULT_CSS = """
    QueuedStrip {
        display: none;
        width: 100%;
        height: auto;
        border-top: solid $rule;
        padding: 0 2;
        color: $orange;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002
        super().__init__("", id=id)
        self._queued: str | None = None

    @property
    def queued(self) -> str | None:
        """The queued message text, or ``None`` when nothing is queued."""
        return self._queued

    @property
    def text(self) -> str:
        """The exact strip line currently displayed (empty when hidden)."""
        return queued_text(self._queued) if self._queued is not None else ""

    def show_queued(self, text: str) -> None:
        """Show the strip for a queued next-turn message."""
        self._queued = text
        self.update(Text(self.text))
        self.display = True

    def clear_queued(self) -> None:
        """Hide the strip (queued message picked up or cancelled)."""
        self._queued = None
        self.update(Text(""))
        self.display = False

    def on_click(self, event: events.Click) -> None:
        event.stop()
        if self._queued is not None:
            self.post_message(self.RecallRequested())


__all__ = ["QUEUED_PREVIEW_CHARS", "RECALL_HINT", "QueuedStrip", "queued_text"]
