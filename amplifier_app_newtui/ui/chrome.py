"""Title bar chrome (DESIGN-SPEC §2 item 1).

Centered title ``amplifier-app-newtui — Amplifier — <state> — <bundle> —
<session-short>`` on the ``bg-chrome`` background. While a turn is
running the title is prefixed with an orange spinner glyph cycling
``✳ ✦ ✧ ✦`` every ~260ms (Textual timer).

The ``<state>`` text is owned by the app: it reflects the current plan
step (lowercased) or ``ready`` / ``planning`` / ``brainstorming`` /
``✳ coordinating N agents`` — the title bar only displays it.
"""

from __future__ import annotations

from textual.content import Content
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import Static

from ..model.blocks import GLYPH_SPINNER_FRAMES

TITLE_SEPARATOR = " — "
SPINNER_INTERVAL = 0.26
"""Seconds between spinner frames (~260ms per DESIGN-SPEC §2)."""

APP_TITLE_NAME = "amplifier-app-newtui"
PRODUCT_NAME = "Amplifier"


class TitleBar(Static):
    """The top chrome strip.

    State API (all reactives; the app sets them, the bar repaints):

    - ``state_text``: the ``<state>`` fragment (``ready``, a plan step, …).
    - ``bundle`` / ``session_short``: identity fragments (skipped when empty).
    - ``running``: True while a turn executes — starts the spinner timer.
    """

    DEFAULT_CSS = """
    TitleBar {
        dock: top;
        width: 100%;
        height: 1;
        background: $bg-chrome;
        color: $dim;
        text-align: center;
    }
    """

    state_text: reactive[str] = reactive("ready")
    bundle: reactive[str] = reactive("")
    session_short: reactive[str] = reactive("")
    running: reactive[bool] = reactive(False)

    def __init__(self, *, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        self._frame_index = 0
        self._spinner_timer: Timer | None = None

    # -- text assembly -----------------------------------------------------

    @property
    def spinner_glyph(self) -> str:
        """The current spinner frame (``✳``/``✦``/``✧``/``✦``)."""
        return GLYPH_SPINNER_FRAMES[self._frame_index % len(GLYPH_SPINNER_FRAMES)]

    def title_text(self) -> str:
        """Plain rendered title, spinner prefix included while running."""
        title = self._plain_title()
        if self.running:
            return f"{self.spinner_glyph} {title}"
        return title

    # -- painting ----------------------------------------------------------

    def _repaint(self) -> None:
        if self.running:
            # Substitution kwargs insert values literally (no markup parse).
            self.update(
                Content.from_markup(
                    "[bold $orange]$glyph[/] $title",
                    glyph=self.spinner_glyph,
                    title=self._plain_title(),
                )
            )
        else:
            self.update(Content.from_markup("$title", title=self._plain_title()))

    def _plain_title(self) -> str:
        parts = [APP_TITLE_NAME, PRODUCT_NAME, self.state_text]
        if self.bundle:
            parts.append(self.bundle)
        if self.session_short:
            parts.append(self.session_short)
        return TITLE_SEPARATOR.join(parts)

    def advance_spinner(self) -> None:
        """Step to the next spinner frame and repaint (timer callback)."""
        self._frame_index = (self._frame_index + 1) % len(GLYPH_SPINNER_FRAMES)
        self._repaint()

    # -- reactive watchers ---------------------------------------------------

    def watch_running(self, running: bool) -> None:
        if running:
            self._frame_index = 0
            if self._spinner_timer is None and self.is_running:
                self._spinner_timer = self.set_interval(
                    SPINNER_INTERVAL, self.advance_spinner
                )
        else:
            if self._spinner_timer is not None:
                self._spinner_timer.stop()
                self._spinner_timer = None
            self._frame_index = 0
        self._repaint()

    def watch_state_text(self, _value: str) -> None:
        self._repaint()

    def watch_bundle(self, _value: str) -> None:
        self._repaint()

    def watch_session_short(self, _value: str) -> None:
        self._repaint()

    def on_mount(self) -> None:
        # If running was set before mount, the timer could not start yet.
        if self.running and self._spinner_timer is None:
            self._spinner_timer = self.set_interval(
                SPINNER_INTERVAL, self.advance_spinner
            )
        self._repaint()

    def on_unmount(self) -> None:
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None


__all__ = [
    "APP_TITLE_NAME",
    "PRODUCT_NAME",
    "SPINNER_INTERVAL",
    "TITLE_SEPARATOR",
    "TitleBar",
]
