"""Transient notice slot (DESIGN-SPEC §2 item 3).

A single-slot, right-aligned dim text line floating at the transcript's
bottom edge: ``mode plan · read-only``, ``steer queued · shift+enter
queues a full next-turn message``, ``approval required · choose below
the transcript``, … Auto-dismisses after ~4 seconds; showing a new
notice replaces the current one and restarts the clock.
"""

from __future__ import annotations

from textual.content import Content
from textual.timer import Timer
from textual.widgets import Static

NOTICE_DURATION = 4.0
"""Seconds a notice stays visible (DESIGN-SPEC §2: auto-dismiss ~4s)."""


class NoticeSlot(Static):
    """The one-and-only notice line.

    The app composes this docked at the bottom edge of the transcript
    region; the widget only manages its own text/visibility/timer.
    """

    DEFAULT_CSS = """
    NoticeSlot {
        width: 100%;
        height: 1;
        color: $dim;
        text-align: right;
        padding: 0 1;
        display: none;
    }
    NoticeSlot.-visible { display: block; }
    """

    def __init__(
        self,
        *,
        duration: float = NOTICE_DURATION,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._duration = duration
        self._current: str | None = None
        self._timer: Timer | None = None

    @property
    def current(self) -> str | None:
        """The visible notice text, or None when the slot is empty."""
        return self._current

    def show_notice(self, text: str) -> None:
        """Show *text*, replacing any current notice and resetting the 4s clock."""
        self._current = text
        # Substitution keeps arbitrary notice text literal (no markup parse).
        self.update(Content.from_markup("$text", text=text))
        self.add_class("-visible")
        if self._timer is not None:
            self._timer.stop()
        self._timer = self.set_timer(self._duration, self.dismiss_notice)

    def dismiss_notice(self) -> None:
        """Clear the slot immediately."""
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._current = None
        self.update("")
        self.remove_class("-visible")

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None


__all__ = ["NOTICE_DURATION", "NoticeSlot"]
