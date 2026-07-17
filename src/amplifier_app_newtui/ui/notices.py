"""Transient notice slot (DESIGN-SPEC §2 item 3).

A single-slot, right-aligned dim text line floating at the transcript's
bottom edge: ``mode plan · read-only``, ``steer queued · shift+enter
queues a full next-turn message``, ``approval required · choose below
the transcript``, … Auto-dismisses after ~4 seconds (callers may pass a
longer per-notice duration, mirroring the mockup's ``showNotice(text,
ms = 4000)``); showing a new notice replaces the current one and
restarts the clock.

The mockup renders the notice as a ``position: absolute`` box inside a
height-0 container (``right: 18px; padding: 0 6px; background:
var(--bg-term)``) so showing/hiding it never moves the transcript and it
covers only its own text width — transcript text elsewhere on the row
stays visible. The Textual equivalent: the slot is an auto-width widget
on its own compositor layer (``layer: notice``); the transcript region
declares ``align: right bottom`` which places each layer's flow widgets
independently, parking the slot over the bottom-right of the last
transcript row without consuming a layout row or blanking the rest of
the row.
"""

from __future__ import annotations

from textual.content import Content
from textual.timer import Timer
from textual.widgets import Static

NOTICE_DURATION = 4.0
"""Seconds a notice stays visible (DESIGN-SPEC §2: auto-dismiss ~4s)."""


class NoticeSlot(Static):
    """The one-and-only notice line.

    The app composes this inside the transcript region, whose stylesheet
    declares a ``notice`` layer and ``align: right bottom``; the widget
    floats over the bottom-right of the region's last row (auto width, so
    it blanks only its own box — mockup ``right: 18px; padding: 0 6px``)
    and only manages its own text/visibility/timer.
    """

    DEFAULT_CSS = """
    NoticeSlot {
        layer: notice;
        width: auto;
        height: 1;
        color: $dim;
        background: $bg-term;
        padding: 0 1;
        margin-right: 2;
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

    def show_notice(self, text: str, duration: float | None = None) -> None:
        """Show *text*, replacing any current notice and restarting the clock.

        *duration* overrides the slot default for this notice only
        (mockup ``showNotice(text, ms = 4000)``; approval notices pass 6s).
        """
        self._current = text
        # Substitution keeps arbitrary notice text literal (no markup parse).
        self.update(Content.from_markup("$text", text=text))
        self.add_class("-visible")
        if self._timer is not None:
            self._timer.stop()
        self._timer = self.set_timer(
            self._duration if duration is None else duration, self.dismiss_notice
        )

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
