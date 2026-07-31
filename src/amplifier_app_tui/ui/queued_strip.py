"""Queued-message overlay strip (DESIGN-SPEC §2/§5).

A one-line orange strip docked ABOVE the composer, shown while a full
next-turn message is queued (Shift+Enter while running, or a second
steer):

``▹ queued next: "<text>" · runs when this turn ends``

The strip is display-only: the SteeringQueue owns the state, the footer
shows the ``· q1`` badge, and the app clears the strip when the queued
message is picked up at turn end.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from ..model.blocks import GLYPH_QUEUED


def queued_text(text: str) -> str:
    """Exact strip text: ``▹ queued next: "<text>" · runs when this turn ends``."""
    return f'{GLYPH_QUEUED} queued next: "{text}" · runs when this turn ends'


class QueuedStrip(Static):
    """The queued-next-message strip (orange, bordered, above composer)."""

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


__all__ = ["QueuedStrip", "queued_text"]
